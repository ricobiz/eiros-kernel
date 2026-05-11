import json
import re
from typing import Dict, List, Optional

import httpx

from config import MODEL, OPENROUTER_KEY
from core.events import EventBus
from core.schemas import AgentOutput, MemoryWrite, Reflection, Task, ToolCall
from memory.store import MemoryLayer


def _extract_json(raw: str) -> dict:
    raw = raw.strip()
    if raw.startswith("```json"):
        raw = raw.split("```json", 1)[1].split("```", 1)[0].strip()
    elif raw.startswith("```"):
        raw = raw.split("```", 1)[1].split("```", 1)[0].strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except Exception:
                pass
        raise ValueError("Could not extract JSON from executor output")


EXECUTOR_SYSTEM = """You are EirosKernel v0.4b Executor. You execute tasks step by step.

CRITICAL RULES:
- Execute ONE plan step per response
- Never claim you did something without a matching action
- Use session_id from previous steps for browser.dom_snapshot, browser.screenshot, browser.close
- If all plan steps are done → mode="answer", no actions
- confidence < 0.5 → set needs_user=true

Response schema (JSON only):
{
  "mode": "act|answer|wait",
  "message": "what you're doing",
  "actions": [{"type": "tool.call", "tool": "tool_name", "args": {...}}],
  "memory_writes": [{"type": "fact|result|error", "content": "...", "tags": [], "importance": 5}],
  "reflection": {"what_i_did": "...", "result": "...", "next_behavior_change": null},
  "confidence": 0.8,
  "needs_user": false,
  "reason": null,
  "question": null
}"""


class ExecutorAgent:
    def __init__(self, event_bus: EventBus, memory: MemoryLayer):
        self.event_bus = event_bus
        self.memory = memory
        self.client = httpx.AsyncClient(timeout=120.0)
        self.browser_steps: Dict[str, int] = {}

    async def build_context(self, task: Task) -> Dict:
        recent = await self.event_bus.get_recent(15)
        relevant_mem = await self.memory.search(task.goal, limit=5)
        return {
            "task": task.model_dump(),
            "recent_events": recent,
            "relevant_memory": relevant_mem,
        }

    def _extract_session_id_from_steps(self, steps: List[Dict]) -> Optional[str]:
        """Find most recent browser session_id from executed steps."""
        for step in reversed(steps):
            if step.get("tool") == "browser.open_url" and step.get("result") == "success":
                # Look in recent events for session_id
                return None  # will be pulled from events in prompt
        return None

    def _build_prompt(self, context: Dict, available_tools: List[str]) -> str:
        task = context["task"]
        recent_events = context.get("recent_events", [])
        relevant_memory = context.get("relevant_memory", [])
        plan = task.get("plan") or []
        steps_done = task.get("steps", [])
        contract = task.get("result_contract") or {}

        # Find active session_id from recent events
        session_id_hint = ""
        for evt in reversed(recent_events):
            if evt.get("type") == "browser.session.created":
                sid = evt.get("payload", {}).get("session_id", "")
                if sid:
                    session_id_hint = f"\nACTIVE BROWSER SESSION: {sid} — use this for dom_snapshot, screenshot, close"
                    break

        events_str = "\n".join(
            f"  [{e.get('type')}] {json.dumps(e.get('payload', {}))[:150]}"
            for e in recent_events[-5:]
        ) or "none"

        memory_str = "\n".join(
            f"  [{m.get('type')}] {m.get('content', '')[:150]}"
            for m in relevant_memory
        ) or "none"

        plan_str = "\n".join(
            f"  {i+1}. {'✓ ' if i < len(steps_done) else '→ ' if i == len(steps_done) else '  '}{step}"
            for i, step in enumerate(plan)
        ) or "  No plan"

        steps_str = "\n".join(
            f"  {s.get('tool')} → {s.get('result')} @ {s.get('ts', '')[:19]}"
            for s in steps_done[-5:]
        ) or "none"

        tools_examples = {
            "browser.open_url": '{"tool": "browser.open_url", "args": {"url": "https://..."}}',
            "browser.dom_snapshot": '{"tool": "browser.dom_snapshot", "args": {"session_id": "brs_xxx"}}',
            "browser.screenshot": '{"tool": "browser.screenshot", "args": {"session_id": "brs_xxx", "path": "screen.png"}}',
            "browser.click": '{"tool": "browser.click", "args": {"session_id": "brs_xxx", "selector": "#btn"}}',
            "browser.type": '{"tool": "browser.type", "args": {"session_id": "brs_xxx", "selector": "#input", "text": "hello"}}',
            "browser.close": '{"tool": "browser.close", "args": {"session_id": "brs_xxx"}}',
            "audio.analyze": '{"tool": "audio.analyze", "args": {"url": "https://cdn.suno.ai/track.mp3"}}',
            "file.write": '{"tool": "file.write", "args": {"path": "out.txt", "content": "..."}}',
            "file.read": '{"tool": "file.read", "args": {"path": "file.txt"}}',
        }
        tools_str = "\n".join(f"  {t}: {tools_examples.get(t, '{...}')}" for t in available_tools)

        return f"""{EXECUTOR_SYSTEM}

=== TASK ===
ID: {task['id']}
Goal: {task['goal']}
Status: {task['status']}
Risk: {task.get('risk', 'low')}
{session_id_hint}

=== PLAN (execute next → step) ===
{plan_str}

=== STEPS EXECUTED ===
{steps_str}

=== RESULT CONTRACT ===
{json.dumps(contract, indent=2) if contract else 'not set'}

=== RELEVANT MEMORY ===
{memory_str}

=== RECENT EVENTS ===
{events_str}

=== AVAILABLE TOOLS ===
{tools_str}

Execute the next → step. If all ✓, set mode="answer"."""

    async def run(self, context: Dict, available_tools: List[str]) -> AgentOutput:
        prompt = self._build_prompt(context, available_tools)
        trace = context.get("trace", {})

        if OPENROUTER_KEY:
            try:
                resp = await self.client.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {OPENROUTER_KEY}",
                        "HTTP-Referer": "https://eiros.local",
                        "X-Title": "EirosKernel-Executor",
                    },
                    json={
                        "model": MODEL,
                        "messages": [{"role": "user", "content": prompt}],
                        "response_format": {"type": "json_object"},
                        "temperature": 0.3,
                        "max_tokens": 2000,
                    },
                )
                resp.raise_for_status()
                raw = resp.json()["choices"][0]["message"]["content"]
                data = _extract_json(raw)
                return AgentOutput(**data)
            except Exception as e:
                from core.events import make_event
                await self.event_bus.append(
                    make_event("system.error", {"error": str(e), "component": "executor", "fallback": True}, context=trace)
                )
                return self._mock_output(context)
        else:
            return self._mock_output(context)

    def _mock_output(self, context: Dict) -> AgentOutput:
        """
        Smart mock: walks the plan step by step.
        Extracts session_id from previous steps for browser tools that need it.
        """
        task = context["task"]
        plan = task.get("plan") or []
        steps_done = task.get("steps", [])
        recent_events = context.get("recent_events", [])
        step_index = len(steps_done)

        # All steps done
        if step_index >= len(plan):
            return AgentOutput(
                mode="answer",
                message="All plan steps completed (mock mode).",
                actions=[],
                memory_writes=[],
                reflection=Reflection(what_i_did="Completed all steps", result="Mock done", next_behavior_change=None),
                confidence=0.5,
            )

        next_step = plan[step_index].lower()

        # Find active session_id from recent events
        session_id = None
        for evt in reversed(recent_events):
            if evt.get("type") in ("browser.session.created", "browser.navigate"):
                session_id = evt.get("payload", {}).get("session_id")
                if session_id:
                    break

        # Map plan step → tool call
        action = None
        message = f"Mock executing step {step_index + 1}: {plan[step_index]}"

        if "open" in next_step and ("url" in next_step or "browser" in next_step or "http" in next_step):
            action = ToolCall(tool="browser.open_url", args={"url": "https://example.com"})
        elif "dom snapshot" in next_step or "snapshot" in next_step:
            if session_id:
                action = ToolCall(tool="browser.dom_snapshot", args={"session_id": session_id})
            else:
                # No session yet — open first
                action = ToolCall(tool="browser.open_url", args={"url": "https://example.com"})
                message = "No session found, opening URL first (mock)"
        elif "screenshot" in next_step:
            if session_id:
                action = ToolCall(tool="browser.screenshot", args={"session_id": session_id, "path": f"screen_{step_index}.png"})
            else:
                action = ToolCall(tool="browser.open_url", args={"url": "https://example.com"})
                message = "No session found, opening URL first (mock)"
        elif "close" in next_step and session_id:
            action = ToolCall(tool="browser.close", args={"session_id": session_id})
        elif "write" in next_step or "save" in next_step or "file" in next_step:
            action = ToolCall(tool="file.write", args={"path": f"output_{step_index}.txt", "content": f"Result of step: {plan[step_index]}"})
        elif "read" in next_step:
            action = ToolCall(tool="file.read", args={"path": "output.txt"})

        if action is None:
            # Unrecognized step → answer
            return AgentOutput(
                mode="answer",
                message=f"Step '{plan[step_index]}' has no mock mapping — skipping (mock mode).",
                actions=[],
                memory_writes=[],
                reflection=Reflection(what_i_did=f"Skipped step: {plan[step_index]}", result="No mock mapping", next_behavior_change=None),
                confidence=0.4,
            )

        return AgentOutput(
            mode="act",
            message=message,
            actions=[action],
            memory_writes=[],
            reflection=Reflection(
                what_i_did=f"Mock step {step_index + 1}/{len(plan)}: {plan[step_index]}",
                result="Mock execution",
                next_behavior_change=None,
            ),
            confidence=0.5,
        )

    async def close(self):
        await self.client.aclose()
