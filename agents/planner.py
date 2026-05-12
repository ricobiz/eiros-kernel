import json
import re
from typing import Optional

import httpx

from config import OPENROUTER_KEY, PLANNER_MODEL
from core.schemas import PlannerOutput, Task, TaskResultContract

PLANNER_PROMPT = """You are EirosKernel Planner. Analyze the task goal and produce an execution plan.

Available tools: {tools}
Task goal: {goal}

Respond ONLY with valid JSON:
{{
  "plan": ["step 1", "step 2", ...],
  "result_contract": {{
    "success_criteria": ["checkable criterion 1", "checkable criterion 2"],
    "required_tools": ["tool_name"],
    "forbidden_tools": [],
    "max_steps": 10,
    "timeout_seconds": 120
  }},
  "risk": "low|medium|high",
  "rationale": "brief explanation"
}}

Rules:
- Plan steps must be concrete actions, not vague intentions
- success_criteria must be checkable, not just "task completed"
- required_tools = tools that MUST be called for task to be considered done
- risk=high if task involves irreversible external actions
- TOOL SELECTION (strictly follow this):
  * If autoagent.run_browser_task is available AND the task involves any web interaction
    beyond a single URL open → use autoagent.run_browser_task as the ONLY browser tool.
    Plan = single step: "Use autoagent.run_browser_task to <goal description>"
    Do NOT add browser.create_session / browser.navigate / browser.dom_snapshot before or after.
  * Only use browser.open_url + browser.screenshot for trivial screenshot-only tasks
    where NO interaction or content extraction is needed.
"""


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
        raise ValueError("Could not extract JSON from planner output")


class PlannerAgent:
    def __init__(self):
        self.client = httpx.AsyncClient(timeout=60.0)
        self._event_bus = None  # injected after init if needed

    def set_event_bus(self, event_bus):
        self._event_bus = event_bus

    async def plan(self, task: Task, available_tools: list) -> PlannerOutput:
        if not OPENROUTER_KEY:
            return self._mock_plan(task, available_tools)

        prompt = PLANNER_PROMPT.format(goal=task.goal, tools=json.dumps(available_tools))
        try:
            resp = await self.client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENROUTER_KEY}",
                    "HTTP-Referer": "https://eiros.local",
                    "X-Title": "EirosKernel-Planner",
                },
                json={
                    "model": PLANNER_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "response_format": {"type": "json_object"},
                    "temperature": 0.2,
                    "max_tokens": 1000,
                },
            )
            resp.raise_for_status()
            raw = resp.json()["choices"][0]["message"]["content"]
            data = _extract_json(raw)
            return PlannerOutput(
                plan=data.get("plan", []),
                result_contract=TaskResultContract(**data.get("result_contract", {})),
                risk=data.get("risk", "low"),
                rationale=data.get("rationale", ""),
            )
        except Exception as e:
            # Log planner failure to event bus
            if self._event_bus:
                from core.events import make_event
                await self._event_bus.append(make_event(
                    "system.error",
                    {"error": str(e), "component": "planner", "task_id": task.id, "fallback": True},
                    source="planner",
                ))
            fallback = self._mock_plan(task, available_tools)
            fallback.rationale = f"[PLANNER FALLBACK: {e}] {fallback.rationale}"
            return fallback

    def _mock_plan(self, task: Task, available_tools: list) -> PlannerOutput:
        goal = task.goal.lower()
        _complex_keywords = (
            "login", "войди", "sign in", "авторизуй", "заполни", "fill",
            "submit", "отправь", "register", "sign up", "captcha", "капча",
            "search", "найди", "find", "download", "скачай", "form",
        )
        _is_complex_browser = any(k in goal for k in _complex_keywords)
        _is_browser = any(k in goal for k in ["browser", "сайт", "http", "open", "navigate", "url"])

        if _is_browser and _is_complex_browser and "autoagent.run_browser_task" in available_tools:
            return PlannerOutput(
                plan=[
                    "Delegate full browser interaction to autoagent.run_browser_task",
                    "Verify returned summary and final_url meet success criteria",
                ],
                result_contract=TaskResultContract(
                    success_criteria=["autoagent completed", "summary returned"],
                    required_tools=["autoagent.run_browser_task"],
                    forbidden_tools=[],
                    max_steps=3,
                    timeout_seconds=300,
                ),
                risk="medium",
                rationale="Complex browser task — delegated to AutoAgent executor.",
            )
        if _is_browser:
            return PlannerOutput(
                plan=[
                    "Open target URL with browser.open_url",
                    "Take DOM snapshot to read page content",
                    "Take screenshot for visual verification",
                    "Close browser session",
                ],
                result_contract=TaskResultContract(
                    success_criteria=["page opened", "dom snapshot captured", "screenshot saved"],
                    required_tools=["browser.open_url", "browser.dom_snapshot", "browser.screenshot"],
                    forbidden_tools=[],
                    max_steps=8,
                    timeout_seconds=60,
                ),
                risk="low",
                rationale="Simple browser task — open, snapshot, screenshot, close.",
            )
        return PlannerOutput(
            plan=["Analyze goal and respond"],
            result_contract=TaskResultContract(
                success_criteria=["task acknowledged"],
                required_tools=[],
                forbidden_tools=[],
                max_steps=3,
                timeout_seconds=30,
            ),
            risk="low",
            rationale="Generic task — answer directly.",
        )

    async def close(self):
        await self.client.aclose()
