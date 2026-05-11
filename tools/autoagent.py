"""
autoagent.run_browser_task — delegates complex browser tasks to RPBOT-AutoAgent.

Registered in main.py as:
    tools.register("autoagent.run_browser_task", run_browser_task)

The executor calls it as a plain async function.
Event emission is handled via the event_bus passed through **kwargs
(the registry passes event_bus and _context for tools prefixed with "autoagent.").
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from integrations.autoagent_bridge import AutoAgentBridge, AutoAgentBridgeError
from core.events import EventBus

logger = logging.getLogger(__name__)

_bridge: Optional[AutoAgentBridge] = None


def _get_bridge() -> AutoAgentBridge:
    global _bridge
    if _bridge is None:
        base_url = os.getenv("AUTOAGENT_URL", "http://localhost:8002")
        api_key = os.getenv("AUTOAGENT_API_KEY")
        _bridge = AutoAgentBridge(base_url=base_url, api_key=api_key)
    return _bridge


async def _emit(event_bus: Optional[EventBus], event_type: str, payload: Dict[str, Any]) -> None:
    if event_bus:
        try:
            await event_bus.append({"type": event_type, "payload": payload})
        except Exception:
            pass


async def run_browser_task(
    goal: Optional[str] = None,
    task: Optional[str] = None,
    max_iters: int = 20,
    headless: bool = True,
    allowed_domains: Optional[List[str]] = None,
    event_bus: Optional[EventBus] = None,
    _context: Optional[Dict[str, Any]] = None,
    **_kwargs: Any,
) -> Dict[str, Any]:
    goal = goal or task  # LLM may pass 'task=' instead of 'goal='
    if not goal:
        return {"error": "goal is required"}

    """
    Delegate a multi-step browser task to RPBOT-AutoAgent.

    Args:
        goal: Natural-language description of what to accomplish.
        max_iters: Max agent iterations (default 20, max 100).
        headless: Run browser headless (default True).
        allowed_domains: Optional domain allowlist.

    Returns dict with: external_task_id, task_status, summary, final_url,
                       iterations, screenshot_b64.
    """
    bridge = _get_bridge()
    ctx = _context or {}
    task_id = ctx.get("task_id")
    request_id = ctx.get("request_id")
    external_task_id: Optional[str] = None

    try:
        create_resp = await bridge.create_task(
            goal,
            max_iters=min(int(max_iters), 100),
            headless=headless,
            allowed_domains=allowed_domains or None,
        )
        external_task_id = create_resp.get("task_id")
        logger.info("AutoAgent task created: %s for kernel task %s", external_task_id, task_id)

        await _emit(event_bus, "autoagent.task.created", {
            "task_id": task_id,
            "request_id": request_id,
            "external_task_id": external_task_id,
            "goal": goal,
        })

        timeout = min(max_iters, 100) * 15.0
        result = await bridge.wait_for_completion(
            external_task_id,
            timeout=timeout,
            poll_interval=3.0,
        )

        status = result.get("status", "unknown")
        event_type = "autoagent.task.completed" if status == "completed" else "autoagent.task.failed"
        await _emit(event_bus, event_type, {
            "task_id": task_id,
            "request_id": request_id,
            "external_task_id": external_task_id,
            "status": status,
            "summary": result.get("summary"),
            "final_url": result.get("final_url"),
            "iterations": result.get("iterations", 0),
        })

        return {
            "external_task_id": external_task_id,
            "task_status": status,
            "summary": result.get("summary"),
            "final_url": result.get("final_url"),
            "iterations": result.get("iterations", 0),
            "screenshot_b64": result.get("screenshot_b64"),
        }

    except AutoAgentBridgeError as e:
        await _emit(event_bus, "autoagent.task.failed", {
            "task_id": task_id,
            "request_id": request_id,
            "external_task_id": external_task_id,
            "error": str(e),
        })
        return {"error": str(e), "external_task_id": external_task_id}
