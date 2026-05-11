import asyncio
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request, WebSocket, WebSocketDisconnect

from config import (
    EIROS_API_KEY, EVENTS_LIMIT_CAP, MAX_INPUT_CHARS,
    MEMORY_SEARCH_LIMIT_CAP, OPENROUTER_KEY, RATE_LIMIT_REQUESTS,
    RATE_LIMIT_WINDOW, WORKSPACE_LIST_LIMIT, WORKSPACE_ROOT,
)
from core.events import ConnectionManager, EventBus, make_event
from core.guard import PermissionGuard
from core.reflection import ReflectionLog
from core.schemas import Event
from core.tasks import TaskScheduler
from core.verifier import VerifierAgent
from agents.executor import ExecutorAgent
from agents.planner import PlannerAgent
from memory.store import MemoryLayer
from tools.registry import ToolRegistry

MAX_LOOP_ITERATIONS = 10


class RateLimiter:
    def __init__(self):
        self._requests: Dict[str, List[float]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def check(self, identifier: str) -> bool:
        async with self._lock:
            now = time.time()
            window_start = now - RATE_LIMIT_WINDOW
            self._requests[identifier] = [t for t in self._requests[identifier] if t > window_start]
            if len(self._requests[identifier]) >= RATE_LIMIT_REQUESTS:
                return False
            self._requests[identifier].append(now)
            return True


rate_limiter = RateLimiter()


def _task_status_from_verdict(verdict: Optional[str], action_results: List[Dict]) -> str:
    if verdict == "pass":
        return "completed"
    if verdict == "partial":
        return "partial"
    if verdict == "fail":
        return "failed"
    if not action_results:
        return "completed"
    return "completed" if all(r.get("status") == "success" for r in action_results) else "failed"


def finalize_response(agent_message: str, tool_results: List[Dict]) -> str:
    if not tool_results:
        return agent_message
    errors = [r for r in tool_results if r.get("status") in ("error", "denied")]
    if errors:
        first = errors[0]
        return f"Partial execution: `{first.get('tool', '?')}` failed: {first.get('error') or first.get('reason', '?')}"
    return agent_message


def build_router(
    event_bus: EventBus,
    manager: ConnectionManager,
    scheduler: TaskScheduler,
    guard: PermissionGuard,
    tools: ToolRegistry,
    memory_layer: MemoryLayer,
    reflection_log: ReflectionLog,
    verifier: VerifierAgent,
    planner: PlannerAgent,
    executor: ExecutorAgent,
) -> APIRouter:

    router = APIRouter()

    async def check_auth(request: Request):
        if not EIROS_API_KEY:
            return
        if request.headers.get("X-API-Key", "") != EIROS_API_KEY:
            raise HTTPException(401, "Invalid or missing X-API-Key")

    @router.websocket("/ws/events")
    async def websocket_events(websocket: WebSocket, api_key: str = Query(default="")):
        if EIROS_API_KEY and api_key != EIROS_API_KEY:
            await websocket.close(code=4001, reason="Unauthorized")
            return
        await manager.connect(websocket)
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            manager.disconnect(websocket)

    @router.post("/message")
    async def handle_message(payload: Dict[str, Any], request: Request):
        await check_auth(request)

        client_ip = request.client.host if request.client else "unknown"
        if not await rate_limiter.check(client_ip):
            raise HTTPException(429, f"Rate limit: {RATE_LIMIT_REQUESTS} req/min")

        text = payload.get("text", "").strip()
        if not text:
            raise HTTPException(400, "text required")
        if len(text) > MAX_INPUT_CHARS:
            raise HTTPException(413, "Input too large")

        request_id = f"req_{uuid.uuid4().hex[:8]}"
        event = Event(type="user.message", payload={"text": text}, context={"request_id": request_id})
        await event_bus.append(event.model_dump())
        ctx = {"task_id": None, "request_id": request_id}

        # === PLANNER ===
        task = await scheduler.create_from_event(event, event_bus)
        ctx["task_id"] = task.id
        available_tools = tools.list_tools()

        plan_output = await planner.plan(task, available_tools)
        await scheduler.set_plan(task.id, plan_output.plan, plan_output.result_contract, plan_output.risk, event_bus, ctx)
        task = scheduler.tasks[task.id]

        if task.id not in executor.browser_steps:
            executor.browser_steps[task.id] = 0

        # === EXECUTOR LOOP with timeout ===
        all_action_results: List[Dict] = []
        last_agent_result = None
        verify_result = None
        task_started = time.monotonic()
        contract_timeout = (
            task.result_contract.timeout_seconds
            if task.result_contract else 120
        )

        for iteration in range(MAX_LOOP_ITERATIONS):
            # Check contract timeout
            elapsed = time.monotonic() - task_started
            if elapsed > contract_timeout:
                await event_bus.append(make_event(
                    "task.timeout",
                    {"task_id": task.id, "elapsed_seconds": round(elapsed, 1), "timeout": contract_timeout},
                    context=ctx,
                ))
                await scheduler.update_status(task.id, "failed", event_bus, ctx)
                return {
                    "response": f"Task timed out after {round(elapsed, 1)}s (contract limit: {contract_timeout}s)",
                    "task_id": task.id, "request_id": request_id,
                    "task_status": "failed", "tool_results": all_action_results,
                    "verify": verify_result,
                }

            context = await executor.build_context(task)
            context["trace"] = ctx
            agent_result = await executor.run(context, available_tools)
            last_agent_result = agent_result

            iteration_results: List[Dict] = []
            for action in agent_result.actions:
                from config import MAX_BROWSER_STEPS_PER_TASK
                if action.tool.startswith("browser."):
                    executor.browser_steps[task.id] += 1
                    if executor.browser_steps[task.id] > MAX_BROWSER_STEPS_PER_TASK:
                        iteration_results.append({"status": "error", "tool": action.tool, "error": "Max browser steps exceeded"})
                        continue

                decision = guard.check(action.model_dump())
                if decision["allowed"]:
                    tool_result = await tools.execute(action.model_dump(), context=ctx)
                    iteration_results.append(tool_result)
                    await scheduler.add_step(
                        task.id,
                        {"tool": action.tool, "args": action.args, "result": tool_result.get("status"), "ts": datetime.now(timezone.utc).isoformat()},
                        event_bus=event_bus, context=ctx,
                    )
                    await event_bus.append(make_event("tool.result", {"action": action.model_dump(), "result": tool_result}, context=ctx))
                else:
                    iteration_results.append({"status": "denied", "tool": action.tool, "reason": decision["reason"]})
                    await event_bus.append(make_event("permission.denied", decision, context=ctx))

            all_action_results.extend(iteration_results)
            task = scheduler.tasks[task.id]

            if iteration_results:
                await event_bus.append(make_event("verifier.check.requested", {"task_id": task.id, "iteration": iteration}, context=ctx))
                verify_result = await verifier.verify(task, all_action_results, event_bus, ctx)

            # Stop conditions
            if agent_result.needs_user:
                break
            if agent_result.mode == "answer":
                break
            if not agent_result.actions:
                break
            if verify_result and verify_result["verdict"] == "pass":
                break

        # === FINAL VERIFIER — always run if contract exists and not yet passed ===
        if task.result_contract and (not verify_result or verify_result["verdict"] != "pass"):
            await event_bus.append(make_event("verifier.check.requested", {"task_id": task.id, "final": True}, context=ctx))
            verify_result = await verifier.verify(task, all_action_results, event_bus, ctx)

        # === STATUS from verifier ===
        if last_agent_result and last_agent_result.needs_user:
            await scheduler.update_status(task.id, "waiting_user", event_bus, ctx)
        else:
            verdict = verify_result["verdict"] if verify_result else None
            await scheduler.update_status(task.id, _task_status_from_verdict(verdict, all_action_results), event_bus, ctx)

        # === MEMORY + REFLECTION ===
        if last_agent_result:
            for mem in last_agent_result.memory_writes:
                await memory_layer.write(mem, source_event=event.id, event_bus=event_bus, context=ctx)
            await reflection_log.write(last_agent_result.reflection, task.id, event_bus, ctx)

        return {
            "response": finalize_response(last_agent_result.message if last_agent_result else "No response", all_action_results),
            "task_id": task.id,
            "request_id": request_id,
            "plan": plan_output.plan,
            "iterations": len(all_action_results),
            "confidence": last_agent_result.confidence if last_agent_result else 0.0,
            "needs_user": last_agent_result.needs_user if last_agent_result else False,
            "question": last_agent_result.question if last_agent_result else None,
            "tool_results": all_action_results,
            "verify": verify_result,
            "browser_steps_used": executor.browser_steps.get(task.id, 0),
            "task_status": scheduler.tasks[task.id].status,
        }

    @router.get("/events")
    async def get_events(request: Request, limit: int = 30):
        await check_auth(request)
        return await event_bus.get_recent(max(1, min(limit, EVENTS_LIMIT_CAP)))

    @router.get("/tasks")
    async def get_tasks(request: Request):
        await check_auth(request)
        return list(scheduler.tasks.values())

    @router.get("/tasks/{task_id}")
    async def get_task(task_id: str, request: Request):
        await check_auth(request)
        if task_id not in scheduler.tasks:
            raise HTTPException(404, "Task not found")
        return scheduler.tasks[task_id]

    @router.get("/memory/search")
    async def search_memory(q: str, request: Request, limit: int = 5):
        await check_auth(request)
        return await memory_layer.search(q, max(1, min(limit, MEMORY_SEARCH_LIMIT_CAP)))

    @router.get("/workspace/list")
    async def list_workspace(request: Request, limit: int = WORKSPACE_LIST_LIMIT):
        await check_auth(request)
        limit = max(1, min(limit, WORKSPACE_LIST_LIMIT))
        files = [
            {"path": str(p.relative_to(WORKSPACE_ROOT)), "size": p.stat().st_size}
            for p in WORKSPACE_ROOT.rglob("*") if p.is_file()
        ][:limit]
        return {"count": len(files), "files": files}

    @router.get("/health")
    async def health():
        from tools.browser import browser_manager
        import config as cfg
        return {
            "status": "alive",
            "version": "0.4b-2026",
            "model": {"executor": cfg.MODEL, "planner": cfg.PLANNER_MODEL},
            "llm_mode": "openrouter" if OPENROUTER_KEY else "mock",
            "auth_enabled": bool(EIROS_API_KEY),
            "events_logged": len(event_bus.events),
            "active_tasks": len(scheduler.tasks),
            "replay_loaded": event_bus.replay_loaded,
            "state_rebuilt": scheduler.state_rebuilt,
            "browser_sessions": len(browser_manager.sessions),
            "max_loop_iterations": MAX_LOOP_ITERATIONS,
        }

    return router
