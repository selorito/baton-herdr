"""The event log stays foldable whatever writers race (ADR 0002): an append that does not fit
the log is refused whole, a run whose task was cancelled under it stops quietly, and one
task's fault does not stop batond's loop."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from baton_herdr.adapters import ADAPTERS
from baton_herdr.core.commands import cancel_task
from baton_herdr.core.config import (
    BatonSettings,
    DatabaseSettings,
    DetectorSettings,
    SchedulerSettings,
    UsageSettings,
)
from baton_herdr.core.events import TaskCompleted, TaskCreated
from baton_herdr.core.fakes import FixedClock, InMemoryEventStore, RecordingNotifier
from baton_herdr.core.model import AgentKind, TaskId, TaskStatus
from baton_herdr.core.notify import NoticeKind
from baton_herdr.core.projection import InvalidEventError, project
from baton_herdr.daemon import open_runtime, run_pending, serve
from baton_herdr.ledger import open_event_store
from baton_herdr.scheduler.runner import RunnerSettings, TaskRunner

from detector_binary import detector, rust_detector
from simulated_agents import SimulatedAgents

if TYPE_CHECKING:
    from pathlib import Path

    from baton_herdr.core.notify import Notice
    from baton_herdr.scheduler.queue import Signature

NOW = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)
TASK = TaskId("t-race")


def created(task_id: TaskId = TASK) -> TaskCreated:
    return TaskCreated(occurred_at=NOW, task_id=task_id, title="x", instructions="x", workdir="/w")


async def test_an_append_that_does_not_fit_the_log_is_refused_whole(tmp_path: Path) -> None:
    db = tmp_path / "baton.db"
    store = await open_event_store(db)
    other = await open_event_store(db)  # another process: batond and the CLI
    try:
        await store.append([created()])
        await other.append([TaskCompleted(occurred_at=NOW, task_id=TASK)])

        # This store has not read the other's append, and still sees it under the lock.
        with pytest.raises(InvalidEventError, match="task is completed"):
            await store.append(
                [created(TaskId("t-new")), TaskCompleted(occurred_at=NOW, task_id=TASK)]
            )
        events = await store.read()
        assert [s.event.type for s in events] == ["task.created", "task.completed"]
        project(events)  # still folds
    finally:
        await store.close()
        await other.close()


class CancelsOnStart(RecordingNotifier):
    """Someone cancels the task while batond watches it: at the moment it started."""

    def __init__(self, store: InMemoryEventStore) -> None:
        super().__init__()
        self._store = store

    async def notify(self, notice: Notice) -> None:
        await super().notify(notice)
        if notice.kind is NoticeKind.TASK_STARTED:
            task = project(await self._store.read()).tasks[notice.task_id]
            await self._store.append(cancel_task(task, at=NOW, reason="not needed"))


async def test_a_task_cancelled_while_it_runs_is_left_cancelled() -> None:
    store = InMemoryEventStore()
    await store.append([created()])
    clock = FixedClock(NOW)
    runner = TaskRunner(
        detector=rust_detector(),
        store=store,
        host=SimulatedAgents(clock, scripts={AgentKind.CLAUDE: "claude-finish.toml"}),
        adapters=ADAPTERS,
        notifier=CancelsOnStart(store),
        clock=clock,
        settings=RunnerSettings(
            agents=(AgentKind.CLAUDE,), start_timeout_s=2, turn_timeout_s=2, poll_interval_s=0.05
        ),
    )

    assert await runner.run(TASK) is TaskStatus.CANCELLED
    project(await store.read())  # nothing the run wrote afterwards broke the log


class Faulty:
    """A runner whose first task always fails, as a bug would."""

    def __init__(self, inner: TaskRunner) -> None:
        self._inner = inner

    async def run(self, task_id: TaskId) -> TaskStatus:
        if task_id == TASK:
            raise RuntimeError("a bug")
        return await self._inner.run(task_id)


async def test_one_tasks_fault_does_not_stop_the_others(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    db = tmp_path / "baton.db"
    store = await open_event_store(db)
    await store.append([created(), created(TaskId("t-fine"))])
    await store.close()
    clock = FixedClock(NOW)
    async with open_runtime(
        BatonSettings(
            database=DatabaseSettings(path=db),
            detector=DetectorSettings(binary=str(detector())),
            usage=UsageSettings(enabled=False),
            scheduler=SchedulerSettings(
                agents=("codex",),
                lean_sessions=False,
                start_timeout_seconds=2,
                turn_timeout_seconds=2,
                poll_interval_seconds=0.05,
            ),
        ),
        host=SimulatedAgents(clock),
        notifier=RecordingNotifier(),
        clock=clock,
    ) as runtime:
        object.__setattr__(runtime, "runner", Faulty(runtime.runner))
        seen: dict[TaskId, Signature] = {}
        assert await run_pending(runtime, seen=seen) == {TaskId("t-fine"): TaskStatus.COMPLETED}
        assert TASK in seen  # not run again until something about it changes
        assert await run_pending(runtime, seen=seen) == {}


class Hangs:
    """A run that does not end on its own: a turn that takes hours."""

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def run(self, task_id: TaskId) -> TaskStatus:
        del task_id
        self.started.set()
        await asyncio.Event().wait()
        return TaskStatus.RUNNING


async def test_stopping_batond_does_not_wait_for_a_turn_to_end(tmp_path: Path) -> None:
    db = tmp_path / "baton.db"
    store = await open_event_store(db)
    await store.append([created()])
    await store.close()
    hangs = Hangs()
    stop = asyncio.Event()
    async with open_runtime(
        BatonSettings(
            database=DatabaseSettings(path=db),
            detector=DetectorSettings(binary=str(detector())),
            usage=UsageSettings(enabled=False),
        ),
        host=SimulatedAgents(FixedClock(NOW)),
        notifier=RecordingNotifier(),
        clock=FixedClock(NOW),
    ) as runtime:
        object.__setattr__(runtime, "runner", hangs)
        serving = asyncio.create_task(serve(runtime, stop=stop, idle_s=3600))
        await asyncio.wait_for(hangs.started.wait(), 5)
        stop.set()
        await asyncio.wait_for(serving, 5)  # not after the turn: at once
