"""cobanD: wires the ports to their real implementations and runs tasks.

This is the composition root. It is the only module that knows which concrete
event store, pane host, adapters and notifier are used; everything below it
depends on the protocols in ``coban.core``.
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING

from coban.adapters import ADAPTERS
from coban.budget.availability import fold_availability
from coban.core.clock import SystemClock
from coban.core.events import TaskCreated
from coban.core.logging import get_logger
from coban.core.model import AgentKind, TaskId, TaskStatus
from coban.core.notify import LoggingNotifier
from coban.core.projection import project
from coban.herdr import connect
from coban.ledger import open_event_store
from coban.scheduler.queue import (
    Signature,
    available_agents,
    next_wake,
    select_tasks,
    task_positions,
)
from coban.scheduler.runner import RunnerSettings, TaskRunner
from coban.telegram.bot import BotSettings, OperatorBot, run_bot
from coban.telegram.notifier import telegram_notifier

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

    from aiogram import Bot

    from coban.core.config import CobanSettings, SchedulerSettings
    from coban.core.events import StoredEvent
    from coban.core.notify import Notifier
    from coban.core.panes import PaneHost
    from coban.core.ports import Clock, EventStore


def runner_settings(settings: SchedulerSettings) -> RunnerSettings:
    return RunnerSettings(
        agents=tuple(AgentKind(agent) for agent in settings.agents),
        limit_cooldown=timedelta(minutes=settings.limit_cooldown_minutes),
        start_timeout_s=settings.start_timeout_seconds,
        turn_timeout_s=settings.turn_timeout_seconds,
        poll_interval_s=settings.poll_interval_seconds,
        timezone=settings.timezone,
        launch_commands={AgentKind(a): cmd for a, cmd in settings.launch_commands.items()},
        max_failure_resumes=settings.max_failure_resumes,
    )


@dataclass(frozen=True, slots=True)
class Runtime:
    store: EventStore
    runner: TaskRunner
    settings: RunnerSettings
    clock: Clock
    host: PaneHost
    # Set when something outside the loop (an operator action) wants a cycle now.
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    operator: OperatorBot | None = None
    bot: Bot | None = None


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
    clock = clock or SystemClock()
    scheduling = runner_settings(settings.scheduler)
    host = host or connect(settings.herdr)
    wake = asyncio.Event()
    try:
        runner = TaskRunner(
            store=store,
            host=host,
            adapters=ADAPTERS,
            notifier=notifier or telegram or LoggingNotifier(),
            clock=clock,
            settings=scheduling,
        )
        operator = None
        if telegram is not None and settings.telegram.owner_id is not None:
            operator = OperatorBot(
                store=store,
                host=host,
                adapters=ADAPTERS,
                clock=clock,
                settings=BotSettings(
                    chat_id=settings.telegram.chat_id or 0,
                    owner_id=settings.telegram.owner_id,
                    agents=scheduling.agents,
                    limit_cooldown=scheduling.limit_cooldown,
                ),
                on_action=wake.set,
            )
        yield Runtime(
            store=store,
            runner=runner,
            settings=scheduling,
            clock=clock,
            host=host,
            wake=wake,
            operator=operator,
            bot=telegram.bot if telegram is not None else None,
        )
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


async def run_pending(
    runtime: Runtime, *, seen: dict[TaskId, Signature] | None = None
) -> dict[TaskId, TaskStatus]:
    """Run, once and oldest first, every task that may make progress now.

    ``seen`` remembers, across calls, the state each task was last run in, so a
    task that is waiting for the same thing is not run (and reported) again.
    """
    seen = {} if seen is None else seen
    events = await runtime.store.read()
    to_run = select_tasks(
        list(project(events).tasks.values()),
        positions=task_positions(events),
        available=_available(runtime, events),
        seen=seen,
    )
    results: dict[TaskId, TaskStatus] = {}
    for task_id in to_run:
        results[task_id] = await runtime.runner.run(task_id)
        events = await runtime.store.read()
        seen[task_id] = (task_positions(events).get(task_id, 0), _available(runtime, events))
    return results


async def serve(
    runtime: Runtime,
    *,
    stop: asyncio.Event,
    idle_s: float = 30,
    max_cycles: int | None = None,
) -> None:
    """Run tasks until ``stop`` is set: the cobanD main loop.

    Between cycles it sleeps until new work may exist: ``idle_s`` to notice new
    tasks, or less when an agent's limit resets sooner.
    """
    seen: dict[TaskId, Signature] = {}
    bot_task = (
        asyncio.create_task(run_bot(runtime.bot, runtime.operator))
        if runtime.bot is not None and runtime.operator is not None
        else None
    )
    if bot_task is not None:
        bot_task.add_done_callback(_bot_stopped)
    try:
        await _serve_loop(runtime, stop=stop, idle_s=idle_s, max_cycles=max_cycles, seen=seen)
    finally:
        if bot_task is not None:
            bot_task.cancel()
            with suppress(asyncio.CancelledError):
                await bot_task


def _bot_stopped(task: asyncio.Task[None]) -> None:
    # Notices still go out through the notifier; only remote actions are gone.
    if not task.cancelled() and (err := task.exception()) is not None:
        get_logger("coban.daemon").error("telegram bot stopped", error=repr(err))


async def _serve_loop(
    runtime: Runtime,
    *,
    stop: asyncio.Event,
    idle_s: float,
    max_cycles: int | None,
    seen: dict[TaskId, Signature],
) -> None:
    cycles = 0
    log = get_logger("coban.daemon")
    while not stop.is_set():
        await run_pending(runtime, seen=seen)
        cycles += 1
        if max_cycles is not None and cycles >= max_cycles:
            break
        availability = fold_availability(
            await runtime.store.read(), cooldown=runtime.settings.limit_cooldown
        )
        now = runtime.clock.now()
        delay = idle_s
        if (wake_at := next_wake(availability, runtime.settings.agents, now)) is not None:
            delay = max(0.0, min(delay, (wake_at - now).total_seconds()))
        log.debug("sleeping", seconds=delay)
        await _sleep(delay, stop, runtime.wake)
        runtime.wake.clear()


async def _sleep(delay: float, *events: asyncio.Event) -> None:
    """Sleep for ``delay`` seconds or until one of ``events`` is set."""
    waiters = [asyncio.create_task(event.wait()) for event in events]
    try:
        await asyncio.wait(waiters, timeout=delay, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for waiter in waiters:
            waiter.cancel()


def _available(runtime: Runtime, events: Sequence[StoredEvent]) -> frozenset[AgentKind]:
    availability = fold_availability(events, cooldown=runtime.settings.limit_cooldown)
    return available_agents(availability, runtime.settings.agents, runtime.clock.now())
