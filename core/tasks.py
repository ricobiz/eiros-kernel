import asyncio
from typing import Dict, List, Optional

from core.events import EventBus, make_event
from core.schemas import Event, Task, TaskResultContract


class TaskScheduler:
    def __init__(self):
        self.tasks: Dict[str, Task] = {}
        self.state_rebuilt: bool = False
        self._lock = asyncio.Lock()

    async def create_from_event(self, event: Event, event_bus: EventBus) -> Task:
        async with self._lock:
            task = Task(
                title=event.payload.get("text", "Unnamed task")[:80],
                goal=event.payload.get("text", ""),
                created_from_event=event.id,
            )
            self.tasks[task.id] = task
            await event_bus.append(
                make_event(
                    "task.created",
                    payload=task.model_dump(),
                    source="scheduler",
                    context={"request_id": event.context.get("request_id")},
                )
            )
            return task

    async def update_status(self, task_id: str, status: str, event_bus: EventBus, context: Optional[Dict] = None):
        async with self._lock:
            if task_id in self.tasks:
                self.tasks[task_id].status = status
                await event_bus.append(
                    make_event(
                        "task.status_changed",
                        payload={"task_id": task_id, "status": status},
                        source="scheduler",
                        context=context or {},
                    )
                )

    async def set_plan(self, task_id: str, plan: List[str], contract: TaskResultContract, risk: str, event_bus: EventBus, context: Optional[Dict] = None):
        async with self._lock:
            if task_id in self.tasks:
                self.tasks[task_id].plan = plan
                self.tasks[task_id].result_contract = contract
                self.tasks[task_id].risk = risk
                await event_bus.append(
                    make_event(
                        "task.planned",
                        payload={
                            "task_id": task_id,
                            "plan": plan,
                            "contract": contract.model_dump(),
                            "risk": risk,
                        },
                        source="planner",
                        context=context or {},
                    )
                )

    async def add_step(self, task_id: str, step: Dict, event_bus: Optional[EventBus] = None, context: Optional[Dict] = None):
        """Append step and emit task.step_added for full replay support."""
        if task_id in self.tasks:
            self.tasks[task_id].steps.append(step)
            if event_bus:
                await event_bus.append(
                    make_event(
                        "task.step_added",
                        payload={"task_id": task_id, "step": step},
                        source="executor",
                        context=context or {},
                    )
                )

    async def rebuild_from_events(self, events: List[Dict]):
        """Replay: task.created → task.planned → task.step_added → task.status_changed."""
        for evt in events:
            etype = evt.get("type")
            payload = evt.get("payload", {})

            if etype == "task.created":
                try:
                    self.tasks[payload["id"]] = Task(**payload)
                except Exception:
                    continue

            elif etype == "task.planned":
                try:
                    tid = payload["task_id"]
                    if tid in self.tasks:
                        self.tasks[tid].plan = payload.get("plan", [])
                        self.tasks[tid].risk = payload.get("risk", "low")
                        contract_data = payload.get("contract")
                        if contract_data:
                            self.tasks[tid].result_contract = TaskResultContract(**contract_data)
                except Exception:
                    continue

            elif etype == "task.step_added":
                try:
                    tid = payload["task_id"]
                    if tid in self.tasks:
                        step = payload.get("step")
                        if step:
                            self.tasks[tid].steps.append(step)
                except Exception:
                    continue

            elif etype == "task.status_changed":
                try:
                    tid = payload["task_id"]
                    if tid in self.tasks:
                        self.tasks[tid].status = payload["status"]
                except Exception:
                    continue

        self.state_rebuilt = True
