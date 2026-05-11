import json
from datetime import datetime, timezone
from typing import Dict, Optional

from config import REFLECTIONS_LOG
from core.events import EventBus, make_event
from core.schemas import Reflection


class ReflectionLog:
    async def write(
        self,
        reflection: Reflection,
        task_id: str,
        event_bus: EventBus,
        context: Optional[Dict] = None,
    ):
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "task_id": task_id,
            "reflection": reflection.model_dump(),
        }
        with open(REFLECTIONS_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        await event_bus.append(
            make_event(
                "reflection.entry",
                reflection.model_dump(),
                source="reflection",
                context=context or {},
            )
        )
