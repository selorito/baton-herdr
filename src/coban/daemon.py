"""cobanD: wires the ports to their real implementations and runs tasks.

This is the composition root. It is the only module that knows which concrete
event store, pane host, adapters and notifier are used; everything below it
depends on the protocols in ``coban.core``.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from coban.adapters import ADAPTERS
from coban.core.clock import SystemClock
from coban.core.events import TaskCreated
from coban.core.model import AgentKind, TaskId, TaskStatus
from coban.core.notify import LoggingNotifier
from coban.core.projection import project
from coban.herdr import connect
from coban.ledger import open_event_store
from coban.scheduler.runner import RunnerSettings, TaskRunner
from coban.telegram.notifier import telegram_notifier

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from coban.core.config import CobanSettings, SchedulerSettings
    from coban.core.notify import Notifier
    from coban.core.panes import PaneHost
    from coban.core.ports import Clock, EventStore
    from coban.core.projection import TaskView


def runner_settings(settings: SchedulerSettings) -> RunnerSettings:
    return RunnerSettings(
        agents=tuple(AgentKind(agent) for agent in settings.agents),
        limit_cooldown=timedelta(minutes=settings.limit_cooldown_minutes),
        start_timeout_s=settings.start_timeout_seconds,
        turn_timeout_s=settings.turn_timeout_seconds,
        poll_interval_s=settings.poll_interval_seconds,
        timezone=settings.timezone,
        launch_commands={AgentKind(a): cmd for a, cmd in settings.launch_commands.items()},
    )


@dataclass(frozen=True, slots=True)
class Runtime:
    store: EventStore
    runner: TaskRunner


@asynccontextmanager
async def open_runtime(
    settings: CobanSettings,
    *,
    host: PaneHost | None = None,
    notifier: Notifier | None = None,
    clock: Clock | None = None,
) -> AsyncIterator[Runtime]:
    """Open the store and connect everything; arguments replace the real parts."""
    store = await open_event_store(settings.database.path)
    telegram = telegram_notifier(settings.telegram) if notifier is None else None
    try:
        runner = TaskRunner(
            store=store,
            host=host or connect(settings.herdr),
            adapters=ADAPTERS,
            notifier=notifier or telegram or LoggingNotifier(),
            clock=clock or SystemClock(),
            settings=runner_settings(settings.scheduler),
        )
        yield Runtime(store=store, runner=runner)
    finally:
        if telegram is not None:
            await telegram.aclose()
        await store.close()


async def add_task(
    store: EventStore,
    *,
    title: str,
    instructions: str,
    workdir: str,
    clock: Clock | None = None,
) -> TaskId:
    task_id = TaskId(f"t-{uuid.uuid4().hex[:8]}")
    now = (clock or SystemClock()).now()
    await store.append(
        [
            TaskCreated(
                occurred_at=now,
                task_id=task_id,
                title=title,
                instructions=instructions,
                workdir=workdir,
            )
        ]
    )
    return task_id


def runnable(tasks: list[TaskView]) -> list[TaskView]:
    """Open tasks without a live attempt, oldest first."""
    return [t for t in tasks if not t.status.is_terminal and t.live_attempt is None]


async def run_pending(runtime: Runtime) -> dict[TaskId, TaskStatus]:
    """Run every runnable task once, in the order they were created."""
    board = project(await runtime.store.read())
    results: dict[TaskId, TaskStatus] = {}
    for task in runnable(list(board.tasks.values())):
        results[task.task_id] = await runtime.runner.run(task.task_id)
    return results
