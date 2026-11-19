"""The dispatcher keeps what its workers found until it is asked (ADR 0017)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

from baton_herdr.core.events import TaskCancelled, TaskCreated
from baton_herdr.core.fakes import FixedClock, InMemoryEventStore
from baton_herdr.core.model import AgentKind, TaskId, TaskStatus
from baton_herdr.scheduler.dispatch import Dispatcher
from baton_herdr.scheduler.runner import RunnerSettings

if TYPE_CHECKING:
    from collections.abc import Sequence

    from baton_herdr.core.events import StoredEvent
    from baton_herdr.scheduler.runner import TaskRunner

NOW = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)


class Runner:
    """Ends ``quick`` at once and keeps every other task running until cancelled."""

    async def run(self, task_id: TaskId) -> TaskStatus:
        if task_id != TaskId("quick"):
            await asyncio.Event().wait()
        return TaskStatus.COMPLETED

    async def agent_slots(self, events: Sequence[StoredEvent]) -> dict[AgentKind, int]:
        del events
        return {}  # the configured slots


async def test_closing_cancels_running_workers_and_keeps_the_results_of_ended_ones() -> None:
    store = InMemoryEventStore()
    await store.append(
        [
            TaskCreated(occurred_at=NOW, task_id=TaskId(n), title=n, instructions=n, workdir=n)
            for n in ("quick", "slow")
        ]
    )
    dispatcher = Dispatcher(
        runner=cast("TaskRunner", Runner()),
        store=store,
        settings=RunnerSettings(),
        clock=FixedClock(NOW),
        seen={},
    )
    assert await dispatcher.start_ready() == (TaskId("quick"), TaskId("slow"))
    await dispatcher.wait_any()

    await dispatcher.aclose()

    assert dispatcher.running == frozenset()
    assert dispatcher.finished() == {TaskId("quick"): TaskStatus.COMPLETED}
    assert dispatcher.finished() == {}


class Waiting:
    """``waiting`` re-attaches to an attempt that still waits for a person: no event;
    ``moved`` ends having appended one."""

    def __init__(self, store: InMemoryEventStore) -> None:
        self._store = store

    async def run(self, task_id: TaskId) -> TaskStatus:
        if task_id == TaskId("moved"):
            await self._store.append([TaskCancelled(occurred_at=NOW, task_id=task_id)])
            return TaskStatus.CANCELLED
        return TaskStatus.NEEDS_HUMAN

    async def agent_slots(self, events: Sequence[StoredEvent]) -> dict[AgentKind, int]:
        del events
        return {}


async def test_a_run_that_changed_nothing_does_not_wake_the_loop() -> None:
    store = InMemoryEventStore()
    await store.append(
        [
            TaskCreated(occurred_at=NOW, task_id=TaskId(n), title=n, instructions=n, workdir=n)
            for n in ("waiting", "moved")
        ]
    )
    dispatcher = Dispatcher(
        runner=cast("TaskRunner", Waiting(store)),
        store=store,
        settings=RunnerSettings(),
        clock=FixedClock(NOW),
        seen={},
    )

    await dispatcher.start_ready(skip={TaskId("moved")})
    await dispatcher.wait_all()
    await asyncio.sleep(0)  # done callbacks run
    assert not dispatcher.changed.is_set()  # it would re-attach at once, in a loop

    await dispatcher.start_ready(skip={TaskId("waiting")})
    await dispatcher.wait_all()
    await asyncio.sleep(0)
    assert dispatcher.changed.is_set()
