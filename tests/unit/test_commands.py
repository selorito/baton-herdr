from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from coban.core.commands import CommandRejectedError, cancel_task, complete_task, fail_task
from coban.core.events import (
    AttemptEnded,
    AttemptInterrupted,
    AttemptStarted,
    Event,
    StoredEvent,
    TaskCancelled,
    TaskCreated,
)
from coban.core.model import (
    AgentKind,
    AttemptId,
    AttemptOutcome,
    AttemptStatus,
    InterruptReason,
    TaskId,
    TaskStatus,
)
from coban.core.projection import Board, apply, project

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
T1 = TaskId("t1")
A1 = AttemptId("a1")


def board_with(*events: Event) -> Board:
    return project(StoredEvent(seq=i, event=e) for i, e in enumerate(events, start=1))


def extend(board: Board, events: list[Event]) -> Board:
    for event in events:
        board = apply(board, StoredEvent(seq=board.last_seq + 1, event=event))
    return board


CREATED = TaskCreated(occurred_at=NOW, task_id=T1, title="t", instructions="-", workdir="/w")
STARTED = AttemptStarted(occurred_at=NOW, task_id=T1, attempt_id=A1, agent=AgentKind.CLAUDE)
LIMITED = AttemptInterrupted(
    occurred_at=NOW, task_id=T1, attempt_id=A1, reason=InterruptReason.RATE_LIMITED
)


@pytest.mark.parametrize("before", [[CREATED, STARTED], [CREATED, STARTED, LIMITED]])
def test_cancelling_ends_the_live_attempt_as_abandoned_first(before: list[Event]) -> None:
    board = board_with(*before)

    events = cancel_task(board.tasks[T1], at=NOW, reason="no longer needed")

    assert [type(e) for e in events] == [AttemptEnded, TaskCancelled]
    assert isinstance(events[0], AttemptEnded)
    assert events[0].outcome is AttemptOutcome.ABANDONED
    task = extend(board, events).tasks[T1]
    assert task.status is TaskStatus.CANCELLED
    assert task.live_attempt is None
    assert task.attempts[0].status is AttemptStatus.ENDED


def test_cancelling_a_task_without_a_live_attempt_is_a_single_event() -> None:
    board = board_with(CREATED)
    events = cancel_task(board.tasks[T1], at=NOW)

    assert [type(e) for e in events] == [TaskCancelled]
    assert extend(board, events).tasks[T1].status is TaskStatus.CANCELLED


@pytest.mark.parametrize(
    ("command", "outcome", "status"),
    [
        (complete_task, AttemptOutcome.SUCCEEDED, TaskStatus.COMPLETED),
        (fail_task, AttemptOutcome.FAILED, TaskStatus.FAILED),
    ],
)
def test_completing_or_failing_closes_the_live_attempt_with_a_matching_outcome(
    command: object, outcome: AttemptOutcome, status: TaskStatus
) -> None:
    board = board_with(CREATED, STARTED)
    assert callable(command)

    task = extend(board, command(board.tasks[T1], at=NOW)).tasks[T1]

    assert task.status is status
    assert task.attempts[0].outcome is outcome
    assert task.live_attempt is None


@pytest.mark.parametrize("command", [cancel_task, complete_task, fail_task])
def test_a_closed_task_cannot_be_closed_again(command: object) -> None:
    board = board_with(CREATED)
    closed = extend(board, cancel_task(board.tasks[T1], at=NOW))
    assert callable(command)

    with pytest.raises(CommandRejectedError, match="already cancelled"):
        command(closed.tasks[T1], at=NOW)


def test_command_events_have_distinct_ids() -> None:
    board = board_with(CREATED, STARTED)
    events = cancel_task(board.tasks[T1], at=NOW)
    assert len({e.event_id for e in events}) == len(events)
    assert uuid4() not in {e.event_id for e in events}
