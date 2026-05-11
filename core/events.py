import asyncio
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Set

from fastapi import WebSocket

from config import EVENTS_LOG


def make_event(
    type_: str,
    payload: Dict[str, Any],
    source: str = "kernel",
    context: Dict[str, Any] = None,
    priority: int = 5,
    requires_response: bool = False,
) -> Dict[str, Any]:
    return {
        "id": f"evt_{uuid.uuid4().hex[:12]}",
        "type": type_,
        "source": source,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "payload": payload,
        "context": context or {},
        "priority": priority,
        "requires_response": requires_response,
        "schema_version": "1.0",
    }


class ConnectionManager:
    def __init__(self):
        self.active_connections: Set[WebSocket] = set()

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.add(websocket)

    def disconnect(self, websocket: WebSocket):
        self.active_connections.discard(websocket)

    async def broadcast(self, event: Dict):
        dead = set()
        for conn in self.active_connections:
            try:
                await conn.send_json(event)
            except Exception:
                dead.add(conn)
        for d in dead:
            self.active_connections.discard(d)


class EventBus:
    def __init__(self, manager: ConnectionManager):
        self.events: List[Dict] = []
        self.replay_loaded: bool = False
        self._lock = asyncio.Lock()
        self._manager = manager

    async def append(self, event: Dict):
        # Write under lock, broadcast AFTER lock — slow WS clients can't block appends
        async with self._lock:
            self.events.append(event)
            with open(EVENTS_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
        # Broadcast outside lock
        await self._manager.broadcast(event)

    async def load_from_log(self):
        import os
        if not os.path.exists(EVENTS_LOG):
            self.replay_loaded = True
            return
        with open(EVENTS_LOG, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    self.events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        self.replay_loaded = True

    async def get_recent(self, limit: int = 20) -> List[Dict]:
        return self.events[-limit:]
