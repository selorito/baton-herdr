"""Properties of the event fold over arbitrary valid event logs."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from hypothesis import given
from hypothesis import strategies as st

from coban.core.events import (
    AgentStateObserved,
    AttemptEnded,
    AttemptInterrupted,
    AttemptResumed,
    AttemptStarted,
    Event,
    StoredEvent,
    TaskCancelled,
    TaskCompleted,
    TaskCreated,
)
from coban.core.fakes import InMemoryEventStore
from coban.core.model import (
    AgentKind,
    AgentState,
    AttemptId,
    AttemptOutcome,
    AttemptStatus,
    InterruptReason,
    ObservationSource,
    TaskId,
    TaskStatus,
)
from coban.core.projection import Board, TaskView, apply, project

START = datetime(2026, 1, 1, tzinfo=UTC)


def _choices(board: Board) -> list[tuple[str, TaskId | None]]:
    """Every kind of event that may validly come next."""
    choices: list[tuple[str, TaskId | None]] = [("create", None)]
    for task in board.tasks.values():
        if task.status.is_terminal:
            continue
        choices.append(("cancel", task.task_id))
        live = task.live_attempt
        if live is None:
            choices += [("start", task.task_id), ("complete", task.task_id)]
        else:
            choices += [("observe", task.task_id), ("end", task.task_id)]
            active = live.status is AttemptStatus.ACTIVE
            choices.append(("interrupt" if active else "resume", task.task_id))
    return choices


def _build(draw: st.DrawFn, kind: str, task: TaskView | None, board: Board, at: datetime) -> Event:
    if task is None:
        new_id = TaskId(f"t{len(board.tasks) + 1}")
        return TaskCreated(
            occurred_at=at, task_id=new_id, title="t", instructions="i", workdir="/w"
        )
    tid = task.task_id
    if kind == "cancel":
        return TaskCancelled(occurred_at=at, task_id=tid)
    if kind == "complete":
        return TaskCompleted(occurred_at=at, task_id=tid)
    if kind == "start":
        return AttemptStarted(
            occurred_at=at,
            task_id=tid,
            attempt_id=AttemptId(f"{tid}-a{len(task.attempts) + 1}"),
            agent=draw(st.sampled_from(AgentKind)),
        )
    live = task.live_attempt
    assert live is not None
    aid = live.attempt_id
    if kind == "observe":
        return AgentStateObserved(
            occurred_at=at,
            task_id=tid,
            attempt_id=aid,
            state=draw(st.sampled_from(AgentState)),
            source=draw(st.sampled_from(ObservationSource)),
        )
    if kind == "interrupt":
        reason = draw(st.sampled_from(InterruptReason))
        return AttemptInterrupted(occurred_at=at, task_id=tid, attempt_id=aid, reason=reason)
    if kind == "resume":
        return AttemptResumed(occurred_at=at, task_id=tid, attempt_id=aid)
    outcome = draw(st.sampled_from(AttemptOutcome))
    return AttemptEnded(occurred_at=at, task_id=tid, attempt_id=aid, outcome=outcome)


@st.composite
def event_logs(draw: st.DrawFn) -> list[StoredEvent]:
    board = Board()
    log: list[StoredEvent] = []
    for index in range(draw(st.integers(min_value=0, max_value=40))):
        kind, task_id = draw(st.sampled_from(_choices(board)))
        task = board.tasks[task_id] if task_id is not None else None
        event = _build(draw, kind, task, board, START + timedelta(seconds=index))
        stored = StoredEvent(seq=index + 1, event=event)
        board = apply(board, stored)
        log.append(stored)
    return log


@given(log=event_logs(), data=st.data())
def test_replaying_a_prefix_then_the_rest_equals_replaying_everything(
    log: list[StoredEvent], data: st.DataObject
) -> None:
    cut = data.draw(st.integers(min_value=0, max_value=len(log)))

    assert project(log) == project(log[cut:], start=project(log[:cut]))
    assert project(log) == project(log)


@given(log=event_logs())
def test_board_invariants_hold_after_every_valid_log(log: list[StoredEvent]) -> None:
    board = project(log)

    assert board.last_seq == len(log)
    for task in board.tasks.values():
        # Only the newest attempt may be unfinished.
        assert all(a.status is AttemptStatus.ENDED for a in task.attempts[:-1])
        assert all(
            (a.outcome is not None) == (a.status is AttemptStatus.ENDED) for a in task.attempts
        )
        assert all(
            (a.interrupt_reason is not None) == (a.status is AttemptStatus.INTERRUPTED)
            for a in task.attempts
        )
        assert len({a.attempt_id for a in task.attempts}) == len(task.attempts)
        live = task.live_attempt
        if task.status is TaskStatus.COMPLETED:
            assert live is None
        if task.status in {TaskStatus.RUNNING, TaskStatus.NEEDS_HUMAN}:
            assert live is not None
            assert live.status is AttemptStatus.ACTIVE
        if task.status is TaskStatus.WAITING:
            assert live is not None
            assert live.status is AttemptStatus.INTERRUPTED
        if task.status is TaskStatus.PENDING:
            assert live is None


@given(log=event_logs())
def test_stored_events_survive_a_json_round_trip(log: list[StoredEvent]) -> None:
    for stored in log:
        assert StoredEvent.model_validate_json(stored.model_dump_json()) == stored


@given(log=event_logs(), data=st.data())
def test_in_memory_store_returns_what_was_appended_in_order(
    log: list[StoredEvent], data: st.DataObject
) -> None:
    cut = data.draw(st.integers(min_value=0, max_value=len(log)))
    events = [stored.event for stored in log]

    async def scenario() -> tuple[list[StoredEvent], list[StoredEvent]]:
        store = InMemoryEventStore()
        await store.append(events[:cut])
        await store.append(events[cut:], expected_seq=cut)
        return list(await store.read()), list(await store.read(after_seq=cut))

    everything, tail = asyncio.run(scenario())

    assert everything == log
    assert tail == log[cut:]
