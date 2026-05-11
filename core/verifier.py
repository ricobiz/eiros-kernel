from datetime import datetime, timezone
from typing import Dict, List, Optional

from core.events import EventBus, make_event
from core.schemas import Task


class VerifierAgent:
    async def verify(
        self,
        task: Task,
        action_results: List[Dict],
        event_bus: EventBus,
        context: Optional[Dict] = None,
    ) -> Dict:
        issues = []
        passed = 0
        failed = 0
        tools_called = set()

        for result in action_results:
            tool = result.get("tool", "unknown")
            status = result.get("status")
            tools_called.add(tool)

            if status == "error":
                failed += 1
                issues.append({"tool": tool, "issue": "tool_error", "detail": result.get("error", "unknown error")})
            elif status == "denied":
                failed += 1
                issues.append({"tool": tool, "issue": "permission_denied", "detail": result.get("reason", "no reason")})
            elif status == "success":
                issue = self._validate_result(tool, result.get("result", {}))
                if issue:
                    issues.append({"tool": tool, **issue})
                    failed += 1
                else:
                    passed += 1

        if task.result_contract:
            contract = task.result_contract

            for req_tool in contract.required_tools:
                if req_tool not in tools_called:
                    failed += 1
                    issues.append({"issue": "required_tool_not_called", "detail": f"{req_tool} required but not called"})

            for forbidden in contract.forbidden_tools:
                if forbidden in tools_called:
                    failed += 1
                    issues.append({"issue": "forbidden_tool_called", "detail": f"{forbidden} is forbidden"})

            # max_steps_exceeded now correctly increments failed
            if len(action_results) > contract.max_steps:
                failed += 1
                issues.append({
                    "issue": "max_steps_exceeded",
                    "detail": f"Used {len(action_results)} steps, max is {contract.max_steps}",
                })

        verdict = "pass" if failed == 0 else ("partial" if passed > 0 else "fail")

        verify_result = {
            "task_id": task.id,
            "verdict": verdict,
            "passed": passed,
            "failed": failed,
            "tools_called": list(tools_called),
            "issues": issues,
            "success_criteria": task.result_contract.success_criteria if task.result_contract else [],
            "ts": datetime.now(timezone.utc).isoformat(),
        }

        await event_bus.append(make_event("verifier.check.completed", verify_result, source="verifier", context=context or {}))
        return verify_result

    def _validate_result(self, tool: str, result: Dict) -> Optional[Dict]:
        if tool == "browser.open_url":
            if not result.get("session_id"):
                return {"issue": "missing_session_id", "detail": "No session_id in result"}
        elif tool == "file.write":
            if not result.get("written_bytes"):
                return {"issue": "zero_bytes_written", "detail": "written_bytes is 0 or missing"}
        elif tool == "browser.screenshot":
            if not result.get("screenshot_path"):
                return {"issue": "no_screenshot_path", "detail": "screenshot_path missing"}
        elif tool == "browser.dom_snapshot":
            if not result.get("text") and not result.get("html"):
                return {"issue": "empty_dom_snapshot", "detail": "DOM snapshot returned empty"}
        return None
