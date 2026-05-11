from typing import Any, Callable, Dict, List, Optional

from core.events import EventBus


class ToolRegistry:
    def __init__(self, event_bus: EventBus):
        self.tools: Dict[str, Callable] = {}
        self.event_bus = event_bus

    def register(self, name: str, fn: Callable):
        self.tools[name] = fn

    async def execute(self, action: Dict, context: Optional[Dict] = None) -> Dict:
        tool_name = action["tool"]
        if tool_name not in self.tools:
            return {
                "status": "error",
                "tool": tool_name,
                "error": f"Tool '{tool_name}' not registered",
            }
        try:
            fn = self.tools[tool_name]
            args = action.get("args", {})
            if tool_name.startswith("browser."):
                result = await fn(**args, event_bus=self.event_bus, _context=context)
            else:
                result = await fn(**args)
            return {"status": "success", "tool": tool_name, "result": result}
        except Exception as e:
            return {"status": "error", "tool": tool_name, "error": str(e)}

    def list_tools(self) -> List[str]:
        return list(self.tools.keys())
