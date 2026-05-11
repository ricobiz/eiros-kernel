#!/usr/bin/env python3
"""
EirosKernel v0.4a — Contract-Driven Execution Loop
Patch fixes from v0.4-rc1:
  - Executor loop (planner → executor tick → tools → verifier → repeat)
  - Task status driven by verifier verdict (pass/partial/fail)
  - Full replay: task.planned + task.step_added restored on restart
  - task.step_added event logged on every add_step
  - WS auth via ?api_key= query param
  - Default host 127.0.0.1 (use EIROS_HOST=0.0.0.0 to expose)
  - health.llm_mode checks OPENROUTER_KEY (not EIROS_API_KEY)
  - browser.close raises ValueError on missing session (not silent fail)
  - DOM snapshot: clickables + inputs with CSS selectors
  - tool not registered returns tool_name in error dict
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config import EIROS_HOST, EIROS_PORT
from core.events import ConnectionManager, EventBus
from core.guard import PermissionGuard
from core.reflection import ReflectionLog
from core.tasks import TaskScheduler
from core.verifier import VerifierAgent
from agents.executor import ExecutorAgent
from agents.planner import PlannerAgent
from memory.store import MemoryLayer
from tools.audio import audio_analyze
from tools.browser import (
    browser_close, browser_create_session, browser_click,
    browser_dom_snapshot, browser_manager, browser_navigate,
    browser_open_url, browser_screenshot, browser_type,
)
from tools.files import file_read, file_write
from tools.registry import ToolRegistry
from api.routes import build_router


# ==================== WIRE ====================
ws_manager = ConnectionManager()
event_bus = EventBus(ws_manager)
scheduler = TaskScheduler()
guard = PermissionGuard()
memory_layer = MemoryLayer()
reflection_log = ReflectionLog()
verifier = VerifierAgent()
planner = PlannerAgent()
planner.set_event_bus(event_bus)  # needed for planner fallback error logging
executor = ExecutorAgent(event_bus, memory_layer)

tools = ToolRegistry(event_bus)
tools.register("file.write", file_write)
tools.register("file.read", file_read)
tools.register("audio.analyze", audio_analyze)
tools.register("browser.open_url", browser_open_url)
tools.register("browser.create_session", browser_create_session)
tools.register("browser.navigate", browser_navigate)
tools.register("browser.click", browser_click)
tools.register("browser.type", browser_type)
tools.register("browser.screenshot", browser_screenshot)
tools.register("browser.dom_snapshot", browser_dom_snapshot)
tools.register("browser.close", browser_close)


# ==================== LIFESPAN ====================
@asynccontextmanager
async def lifespan(app: FastAPI):
    await memory_layer.init()
    await event_bus.load_from_log()
    await scheduler.rebuild_from_events(event_bus.events)
    yield
    await executor.close()
    await planner.close()
    await memory_layer.close()
    if browser_manager.browser:
        await browser_manager.browser.close()
    if browser_manager.playwright:
        await browser_manager.playwright.stop()


# ==================== APP ====================
app = FastAPI(
    title="EirosKernel",
    version="0.4a-2026",
    description="Contract-driven trusted execution kernel for AI agents",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(
    build_router(
        event_bus=event_bus,
        manager=ws_manager,
        scheduler=scheduler,
        guard=guard,
        tools=tools,
        memory_layer=memory_layer,
        reflection_log=reflection_log,
        verifier=verifier,
        planner=planner,
        executor=executor,
    )
)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=EIROS_HOST, port=EIROS_PORT, reload=False)
