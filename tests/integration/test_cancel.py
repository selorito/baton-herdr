"""Dropping work from the phone or the terminal: a task is cancelled and its agent's pane
closed (only a pane verified to host it); a project is dropped whole; a project whose step
did not complete stops instead of waiting for ever."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from baton_herdr.adapters import ADAPTERS
from baton_herdr.core.commands import cancel_task
from baton_herdr.core.events import (
    AgentStateObserved,
    AttemptLocated,
    AttemptStarted,
    PlanProposed,
    PlanStep,
    TaskCompleted,
    TaskCreated,
)
from baton_herdr.core.fakes import FakePaneHost, FixedClock, InMemoryEventStore
from baton_herdr.core.model import (
    AgentKind,
    AgentState,
    AttemptId,
    ObservationSource,
    TaskId,
    TaskStatus,
)
from baton_herdr.core.panes import PaneObservation
from baton_herdr.core.projection import project
from baton_herdr.projects.flow import cancel_project, halt_stopped_projects
from baton_herdr.scheduler import notices
from baton_herdr.scheduler.operator import ActionRefusedError, cancel
from baton_herdr.telegram.bot import BotSettings, OperatorBot
from baton_herdr.telegram.notifier import notice_buttons

NOW = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)
CLOCK = FixedClock(NOW)
TASK = TaskId("t-busy")
ATTEMPT = AttemptId(UUID(int=7))


def created(task_id: TaskId, **fields: object) -> TaskCreated:
    return TaskCreated(
        occurred_at=NOW,
        task_id=task_id,
        title=str(task_id),
        instructions="x",
        workdir="/p",
        **fields,  # type: ignore[arg-type]
    )


async def running(session: str = "s1") -> tuple[InMemoryEventStore, FakePaneHost]:
    """A task whose Claude works in pane p1; the pane hosts session ``session``."""
    store = InMemoryEventStore()
    await store.append(
        [
            created(TASK),
            AttemptStarted(
                occurred_at=NOW, task_id=TASK, attempt_id=ATTEMPT, agent=AgentKind.CLAUDE
            ),
            AttemptLocated(
                occurred_at=NOW, task_id=TASK, attempt_id=ATTEMPT, pane_id="p1", session_ref="s1"
            ),
            AgentStateObserved(
                occurred_at=NOW,
                task_id=TASK,
                attempt_id=ATTEMPT,
                state=AgentState.WORKING,
                source=ObservationSource.HERDR,
            ),
        ]
    )
    host = FakePaneHost()
    host.set_observation(
        PaneObservation(
            pane_id="p1",
            agent=AgentKind.CLAUDE,
            state=AgentState.WORKING,
            evidence="test",
            session_ref=session,
        )
    )
    return store, host


async def test_a_cancelled_task_s_pane_is_closed() -> None:
    store, host = await running()
    assert await cancel(store=store, host=host, clock=CLOCK, task_id=TASK, by="telegram:7")
    assert "p1" not in host.observations  # the pane is closed
    task = project(await store.read()).tasks[TASK]
    assert task.status is TaskStatus.CANCELLED

    with pytest.raises(ActionRefusedError, match="already cancelled"):
        await cancel(store=store, host=host, clock=CLOCK, task_id=TASK, by="cli")
    with pytest.raises(ActionRefusedError, match="no task"):
        await cancel(store=store, host=host, clock=CLOCK, task_id=TaskId("t-x"), by="cli")


async def test_a_pane_that_hosts_another_session_is_left_open() -> None:
    store, host = await running(session="someone-else")
    assert not await cancel(store=store, host=host, clock=CLOCK, task_id=TASK, by="cli")
    assert "p1" in host.observations  # not its pane: left open
    assert project(await store.read()).tasks[TASK].status is TaskStatus.CANCELLED


STEPS = (PlanStep(title="one", instructions="x"), PlanStep(title="two", instructions="x"))


async def project_with(*steps: TaskCreated, proposed: bool = False) -> InMemoryEventStore:
    store = InMemoryEventStore()
    plan = created(TaskId("t-plan"), project="p-1", role="plan", request="a cafe page")
    await store.append([plan, TaskCompleted(occurred_at=NOW, task_id=TaskId("t-plan"))])
    if proposed:
        await store.append(
            [PlanProposed(occurred_at=NOW, task_id=TaskId("t-plan"), summary="s", steps=STEPS)]
        )
    if steps:
        await store.append(list(steps))
    return store


def step(n: int, after: int | None = None) -> TaskCreated:
    return created(
        TaskId(f"t-s{n}"),
        project="p-1",
        role="step",
        after=TaskId(f"t-s{after}") if after else None,
    )


async def test_a_project_is_dropped_whole() -> None:
    store = await project_with(proposed=True)
    assert await cancel_project(store, FakePaneHost(), project_id="p-1", by="cli", clock=CLOCK) == 0
    assert project(await store.read()).tasks[TaskId("t-plan")].plan_rejected

    store = await project_with(step(1), step(2, after=1))
    count = await cancel_project(store, FakePaneHost(), project_id="p-1", by="cli", clock=CLOCK)
    assert count == 2
    board = project(await store.read())
    assert {board.tasks[TaskId(f"t-s{n}")].status for n in (1, 2)} == {TaskStatus.CANCELLED}


async def test_a_project_whose_step_stopped_drops_the_rest_and_says_so_once() -> None:
    store = await project_with(step(1), step(2, after=1), step(3, after=2))
    first = project(await store.read()).tasks[TaskId("t-s1")]
    await store.append(cancel_task(first, at=NOW, reason="not needed"))

    (notice,) = await halt_stopped_projects(store, clock=CLOCK)
    assert notice.label == "⚠ Project stopped"
    assert notice.title == "a cafe page"
    assert "2 step(s) after it were cancelled" in notice.text
    board = project(await store.read())
    assert {board.tasks[TaskId(f"t-s{n}")].status for n in (2, 3)} == {TaskStatus.CANCELLED}
    assert await halt_stopped_projects(store, clock=CLOCK) == []


def bot(store: InMemoryEventStore, host: FakePaneHost) -> OperatorBot:
    return OperatorBot(
        store=store,
        host=host,
        adapters=ADAPTERS,
        clock=CLOCK,
        settings=BotSettings(
            chat_id=1,
            owner_id=7,
            agents=(AgentKind.CLAUDE,),
            limit_cooldown=timedelta(hours=1),
        ),
    )


async def test_telegram_cancels_a_task_by_command_or_by_its_button() -> None:
    store, host = await running()
    reply = await bot(store, host).command(f"/cancel {TASK}")
    assert reply == f"Task cancelled. {TASK}; its pane is closed."
    assert (await bot(store, host).command(f"/cancel {TASK}")).endswith("already cancelled.")
    assert (await bot(store, host).command("/cancel")).startswith("Say what to cancel")

    # A prompt nothing can answer from the phone comes with a Cancel button.
    store, host = await running()
    task = project(await store.read()).tasks[TASK]
    stuck = notices.needs_human(task, "claude is waiting for a decision.")
    assert notice_buttons(stuck) == [("Cancel task", f"task:cancel:{TASK}")]
    assert (await bot(store, host).button(f"task:cancel:{TASK}")).startswith("Task cancelled.")

    store = await project_with(step(1), step(2, after=1))
    reply = await bot(store, FakePaneHost()).command("/cancel p-1")
    assert reply == "Project p-1 cancelled: 2 task(s) dropped."
