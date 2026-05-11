import json
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional

import aiosqlite

from config import DB_PATH
from core.events import EventBus, make_event
from core.schemas import MemoryWrite


class MemoryLayer:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._conn: Optional[aiosqlite.Connection] = None

    async def init(self):
        self._conn = await aiosqlite.connect(self.db_path)
        await self._conn.execute(
            "CREATE TABLE IF NOT EXISTS memory "
            "(id TEXT PRIMARY KEY, type TEXT, content TEXT, tags TEXT, "
            "importance INTEGER, created_at TEXT, source_event TEXT)"
        )
        await self._conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts "
            "USING fts5(id UNINDEXED, content, tags, tokenize='porter ascii')"
        )
        await self._conn.commit()

    async def write(
        self,
        mem: MemoryWrite,
        source_event: str,
        event_bus: EventBus,
        context: Optional[Dict] = None,
    ) -> str:
        mem_id = f"mem_{uuid.uuid4().hex[:12]}"
        tags_json = json.dumps(mem.tags)
        tags_str = " ".join(mem.tags)
        await self._conn.execute(
            "INSERT INTO memory VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                mem_id, mem.type, mem.content, tags_json,
                mem.importance, datetime.now(timezone.utc).isoformat(), source_event,
            ),
        )
        await self._conn.execute(
            "INSERT INTO memory_fts (id, content, tags) VALUES (?, ?, ?)",
            (mem_id, mem.content, tags_str),
        )
        await self._conn.commit()
        await event_bus.append(
            make_event(
                "memory.write",
                {"memory_id": mem_id, "memory": mem.model_dump()},
                source="memory",
                context=context or {},
            )
        )
        return mem_id

    async def search(self, query: str, limit: int = 5) -> List[Dict]:
        results = []
        # FTS first
        try:
            cursor = await self._conn.execute(
                "SELECT m.id, m.type, m.content, m.tags, m.importance, m.created_at "
                "FROM memory m JOIN memory_fts fts ON m.id = fts.id "
                "WHERE memory_fts MATCH ? ORDER BY m.importance DESC LIMIT ?",
                (query, limit),
            )
            rows = await cursor.fetchall()
            if rows:
                results = [
                    {
                        "id": r[0], "type": r[1], "content": r[2],
                        "tags": json.loads(r[3]), "importance": r[4], "created_at": r[5],
                    }
                    for r in rows
                ]
        except Exception:
            pass

        if not results:
            cursor = await self._conn.execute(
                "SELECT id, type, content, tags, importance, created_at FROM memory "
                "WHERE content LIKE ? OR type LIKE ? ORDER BY importance DESC LIMIT ?",
                (f"%{query}%", f"%{query}%", limit),
            )
            rows = await cursor.fetchall()
            results = [
                {
                    "id": r[0], "type": r[1], "content": r[2],
                    "tags": json.loads(r[3]), "importance": r[4], "created_at": r[5],
                }
                for r in rows
            ]
        return results

    async def close(self):
        if self._conn:
            await self._conn.close()
