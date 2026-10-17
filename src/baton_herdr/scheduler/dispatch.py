"""Tasks side by side (ADR 0017): one worker per task, as many as there are free slots.

A worker is an asyncio task that drives one task with :class:`TaskRunner.run` until it
is done, has to wait, or needs a person. The dispatcher starts the tasks that may start
now (``queue.select_tasks``, then ``queue.dispatch``), remembers what each task was last
run in (``seen``), and says when a worker has ended, so a freed slot is filled at once.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import TYPE_CHECKING

from baton_herdr.budget.availability import fold_availability
from baton_herdr.core.logging import get_logger
from baton_herdr.core.projection import project
from baton_herdr.scheduler.queue import available_agents, dispatch, select_tasks, task_positions

if TYPE_CHECKING:
    from collections.abc import Collection, MutableMapping, Sequence

    from baton_herdr.core.events import StoredEvent
    from baton_herdr.core.model import AgentKind, TaskId, TaskStatus
    from baton_herdr.core.ports import Clock, EventStore
    from baton_herdr.scheduler.queue import Signature
    from baton_herdr.scheduler.runner import RunnerSettings, TaskRunner


class Dispatcher:
    def __init__(
        self,
        *,
        runner: TaskRunner,
        store: EventStore,
        settings: RunnerSettings,
        clock: Clock,
        seen: MutableMapping[TaskId, Signature],
    ) -> None:
        self._runner = runner
        self._store = store
        self._settings = settings
        self._clock = clock
        # What each task was last run in; a task is run again only once that changes.
        self._seen = seen
        self._workers: dict[TaskId, asyncio.Task[TaskStatus | None]] = {}
        # Set when a worker ends: a slot is free, or a task's follower may start.
        self.changed = asyncio.Event()
        self._log = get_logger("baton.dispatch")

    @property
    def running(self) -> frozenset[TaskId]:
        return frozenset(t for t, worker in self._workers.items() if not worker.done())

    async def start_ready(self, *, skip: Collection[TaskId] = ()) -> tuple[TaskId, ...]:
        """Start a worker for every task that may start now, except ``skip``."""
        events = await self._store.read()
        board = project(events)
        available = self.available(events)
        candidates = select_tasks(
            list(board.tasks.values()),
            positions=task_positions(events),
            available=available,
            seen=self._seen,
        )
        started = dispatch(
            [c for c in candidates if c not in skip],
            board.tasks,
            running=self.running,
            available=available,
            slots=self._settings.agent_slots,
            default_slots=self._settings.default_agent_slots,
            max_parallel=self._settings.max_parallel_tasks,
        )
        for task_id in started:
            worker = asyncio.create_task(self._work(task_id), name=f"baton-task-{task_id}")
            worker.add_done_callback(lambda _: self.changed.set())
            self._workers[task_id] = worker
        if started:
            self._log.info("tasks started", tasks=list(started), running=len(self.running))
        return started

    def finished(self) -> dict[TaskId, TaskStatus]:
        """The workers that have ended since the last call, and how their tasks stand. A
        worker whose run failed is left out: its fault is logged."""
        results: dict[TaskId, TaskStatus] = {}
        for task_id, worker in list(self._workers.items()):
            if not worker.done():
                continue
            del self._workers[task_id]
            if not worker.cancelled() and (status := worker.result()) is not None:
                results[task_id] = status
        return results

    async def wait_any(self) -> None:
        """Until one worker has ended; at once when none runs."""
        running = [w for w in self._workers.values() if not w.done()]
        if running:
            await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)

    async def wait_all(self) -> None:
        while running := [w for w in self._workers.values() if not w.done()]:
            await asyncio.wait(running)

    async def aclose(self) -> None:
        """Cancel every worker. Their attempts stay active in the log; the next start
        re-attaches to them."""
        for worker in self._workers.values():
            worker.cancel()
        for worker in self._workers.values():
            with suppress(asyncio.CancelledError):
                await worker
        self._workers.clear()

    def available(self, events: Sequence[StoredEvent]) -> frozenset[AgentKind]:
        availability = fold_availability(events, cooldown=self._settings.limit_cooldown)
        return available_agents(availability, self._settings.agents, self._clock.now())

    async def _work(self, task_id: TaskId) -> TaskStatus | None:
        status: TaskStatus | None = None
        try:
            status = await self._runner.run(task_id)
        except Exception:  # noqa: BLE001 - one task's fault must not stop the others
            # Marked seen below: it is not run again until something about it changes, so
            # a fault that repeats cannot loop batond.
            self._log.exception("task run failed", task_id=task_id)
        events = await self._store.read()
        self._seen[task_id] = (task_positions(events).get(task_id, 0), self.available(events))
        return status
