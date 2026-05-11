"""
AutoAgentBridge — async HTTP client for EirosKernel ↔ RPBOT-AutoAgent integration.

Usage (from EirosKernel or any supervisor):

    bridge = AutoAgentBridge(base_url="http://rpbot:8002", api_key="...")
    task = await bridge.create_task("Open example.com, take a screenshot", max_iters=15)
    result = await bridge.wait_for_completion(task["task_id"], timeout=300)
    print(result["summary"], result["final_url"])

The bridge is intentionally thin — it never interprets results, only ferries data
between the supervisor (EirosKernel) and the executor (RPBOT browser agent).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger(__name__)

# Task statuses that mean the run has ended (no point polling further).
_TERMINAL_STATUSES = {"completed", "failed", "stopped", "error", "done"}


class AutoAgentBridgeError(Exception):
    pass


class AutoAgentBridge:
    """
    Async HTTP bridge to RPBOT-AutoAgent's /api/browser/* endpoints.

    All methods raise AutoAgentBridgeError on non-2xx responses.
    """

    def __init__(
        self,
        base_url: str,
        api_key: Optional[str] = None,
        timeout: float = 30.0,
    ):
        self.base_url = base_url.rstrip("/")
        self._headers: Dict[str, str] = {}
        if api_key:
            self._headers["X-API-Key"] = api_key
        self._timeout = timeout

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            headers=self._headers,
            timeout=self._timeout,
        )

    async def _get(self, path: str, **params: Any) -> Dict[str, Any]:
        async with self._client() as c:
            r = await c.get(f"{self.base_url}{path}", params=params or None)
            if r.status_code >= 400:
                raise AutoAgentBridgeError(f"GET {path} → {r.status_code}: {r.text[:200]}")
            return r.json()

    async def _post(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        async with self._client() as c:
            r = await c.post(f"{self.base_url}{path}", json=body)
            if r.status_code >= 400:
                raise AutoAgentBridgeError(f"POST {path} → {r.status_code}: {r.text[:200]}")
            return r.json()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def create_task(
        self,
        goal: str,
        *,
        max_iters: int = 20,
        headless: bool = True,
        planner_model_id: Optional[str] = None,
        allowed_domains: Optional[list] = None,
        proxy: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Start a browser task on RPBOT-AutoAgent.

        Returns {"ok": True, "task_id": "..."}
        """
        body: Dict[str, Any] = {"goal": goal, "max_iters": max_iters, "headless": headless}
        if planner_model_id:
            body["planner_model_id"] = planner_model_id
        if allowed_domains:
            body["allowed_domains"] = allowed_domains
        if proxy:
            body["proxy"] = proxy
        result = await self._post("/api/browser/task", body)
        logger.info("AutoAgent task created: %s", result.get("task_id"))
        return result

    async def get_status(self, task_id: str) -> Dict[str, Any]:
        """
        Lightweight status poll.

        Returns {"task_id", "status", "iterations", "last_error", ...}
        Statuses: running | planning | completed | failed | stopped | paused | waiting_user_hint
        """
        return await self._get(f"/api/browser/task/{task_id}/status")

    async def get_result(self, task_id: str) -> Dict[str, Any]:
        """
        Full result payload — call after status is terminal.

        Returns {"task_id", "status", "summary", "final_url", "screenshot_b64",
                 "logs", "artifacts", "iterations", ...}
        """
        return await self._get(f"/api/browser/task/{task_id}/result")

    async def get_events(self, task_id: str, limit: int = 200) -> Dict[str, Any]:
        """All log events for a task — for EirosKernel audit / replay."""
        return await self._get(f"/api/browser/task/{task_id}/events", limit=limit)

    async def stop_task(self, task_id: str) -> Dict[str, Any]:
        """Request graceful stop of a running task."""
        return await self._post(f"/api/browser/task/{task_id}/control", {"action": "stop"})

    async def send_hint(self, task_id: str, hint: str) -> Dict[str, Any]:
        """Inject a hint into a running agent's context (live guidance)."""
        return await self._post(f"/api/browser/task/{task_id}/hint", {"hint": hint})

    async def get_screenshot(self, task_id: str) -> Dict[str, Any]:
        """Returns {"screenshot_b64": "...", "path": "..."}"""
        return await self._get(f"/api/browser/task/{task_id}/screenshot")

    async def health(self) -> Dict[str, Any]:
        """Check if RPBOT-AutoAgent is reachable."""
        return await self._get("/api/browser/health")

    async def wait_for_completion(
        self,
        task_id: str,
        *,
        timeout: float = 300.0,
        poll_interval: float = 3.0,
    ) -> Dict[str, Any]:
        """
        Poll until task reaches a terminal status or timeout.

        Returns the full result dict on completion.
        Raises AutoAgentBridgeError on timeout.
        """
        elapsed = 0.0
        status_doc: Dict[str, Any] = {}
        while elapsed < timeout:
            try:
                status_doc = await self.get_status(task_id)
            except AutoAgentBridgeError as e:
                logger.warning("Status poll error (will retry): %s", e)
                await asyncio.sleep(poll_interval)
                elapsed += poll_interval
                continue

            status = status_doc.get("status", "")
            logger.debug("Task %s status=%s iter=%s", task_id, status, status_doc.get("iterations"))

            if status in _TERMINAL_STATUSES:
                return await self.get_result(task_id)

            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

        raise AutoAgentBridgeError(
            f"Task {task_id} did not complete within {timeout}s "
            f"(last status: {status_doc.get('status', '?')})"
        )
