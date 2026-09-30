from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

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
from coban.core.projection import Board, InvalidEventError, apply, project

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
T1 = TaskId("t1")
A1 = AttemptId("a1")
A2 = AttemptId("a2")


def log(*events: Event) -> list[StoredEvent]:
    return [StoredEvent(seq=i, event=e) for i, e in enumerate(events, start=1)]


def created(task: TaskId = T1) -> TaskCreated:
    return TaskCreated(
        occurred_at=NOW, task_id=task, title="add power", instructions="-", workdir="/w"
    )


def started(attempt: AttemptId = A1, agent: AgentKind = AgentKind.CLAUDE) -> AttemptStarted:
    return AttemptStarted(
        occurred_at=NOW, task_id=T1, attempt_id=attempt, agent=agent, pane_id="w1:p6"
    )


def observed(state: AgentState, attempt: AttemptId = A1) -> AgentStateObserved:
    return AgentStateObserved(
        occurred_at=NOW, task_id=T1, attempt_id=attempt, state=state, source=ObservationSource.HERDR
    )


def interrupted(reason: InterruptReason, attempt: AttemptId = A1) -> AttemptInterrupted:
    return AttemptInterrupted(
        occurred_at=NOW,
        task_id=T1,
        attempt_id=attempt,
        reason=reason,
        resume_not_before=NOW + timedelta(hours=2),
    )


def resumed(attempt: AttemptId = A1) -> AttemptResumed:
    return AttemptResumed(occurred_at=NOW, task_id=T1, attempt_id=attempt)


def ended(outcome: AttemptOutcome, attempt: AttemptId = A1) -> AttemptEnded:
    return AttemptEnded(occurred_at=NOW, task_id=T1, attempt_id=attempt, outcome=outcome)


FAILED = ended(AttemptOutcome.FAILED)
CRASH = interrupted(InterruptReason.CRASHED)
COMPLETED = TaskCompleted(occurred_at=NOW, task_id=T1)
CANCELLED = TaskCancelled(occurred_at=NOW, task_id=T1)


def test_task_moves_through_a_rate_limited_attempt_to_completion() -> None:
    events = log(
        created(),
        started(),
        observed(AgentState.WORKING),
        interrupted(InterruptReason.RATE_LIMITED),
        resumed(),
        observed(AgentState.WORKING),
        ended(AttemptOutcome.SUCCEEDED),
        TaskCompleted(occurred_at=NOW, task_id=T1),
    )
    statuses = []
    board = Board()
    for stored in events:
        board = apply(board, stored)
        statuses.append(board.tasks[T1].status)

    assert statuses == [
        TaskStatus.PENDING,
        TaskStatus.RUNNING,
        TaskStatus.RUNNING,
        TaskStatus.WAITING,
        TaskStatus.RUNNING,
        TaskStatus.RUNNING,
        TaskStatus.PENDING,
        TaskStatus.COMPLETED,
    ]
    assert board.last_seq == 8
    attempt = board.tasks[T1].attempts[0]
    assert (attempt.status, attempt.outcome) == (AttemptStatus.ENDED, AttemptOutcome.SUCCEEDED)


def test_interruption_records_the_reason_and_resume_time_until_resumed() -> None:
    waiting = project(log(created(), started(), interrupted(InterruptReason.RATE_LIMITED)))
    attempt = waiting.tasks[T1].live_attempt
    assert attempt is not None
    assert attempt.interrupt_reason is InterruptReason.RATE_LIMITED
    assert attempt.resume_not_before == NOW + timedelta(hours=2)

    running = apply(waiting, StoredEvent(seq=4, event=resumed()))
    attempt = running.tasks[T1].live_attempt
    assert attempt is not None
    assert (attempt.interrupt_reason, attempt.resume_not_before) == (None, None)
    assert attempt.agent_state is AgentState.UNKNOWN


@pytest.mark.parametrize(
    "state",
    [AgentState.BLOCKED_PERMISSION, AgentState.BLOCKED_QUESTION, AgentState.BLOCKED_OTHER],
)
def test_a_blocked_agent_makes_the_task_need_a_human(state: AgentState) -> None:
    board = project(log(created(), started(), observed(state)))
    assert board.tasks[T1].status is TaskStatus.NEEDS_HUMAN


def test_a_failed_attempt_returns_the_task_to_pending_and_allows_another_agent() -> None:
    board = project(
        log(
            created(),
            started(A1, AgentKind.CLAUDE),
            ended(AttemptOutcome.FAILED),
            started(A2, AgentKind.CODEX),
        )
    )
    task = board.tasks[T1]
    assert task.status is TaskStatus.RUNNING
    assert [a.agent for a in task.attempts] == [AgentKind.CLAUDE, AgentKind.CODEX]
    assert task.live_attempt is task.attempts[1]


def test_apply_does_not_modify_the_board_it_was_given() -> None:
    before = project(log(created()))
    after = apply(before, StoredEvent(seq=2, event=started()))

    assert before.tasks[T1].attempts == ()
    assert before.last_seq == 1
    assert after.last_seq == 2


@pytest.mark.parametrize(
    ("events", "reason"),
    [
        ([started()], "unknown task"),
        ([created(), created()], "task already exists"),
        ([created(), started(), started(A2)], "another attempt is still live"),
        (
            [created(), started(), ended(AttemptOutcome.FAILED), started()],
            "attempt id already used",
        ),
        ([created(), observed(AgentState.IDLE)], "not the live attempt"),
        ([created(), started(), observed(AgentState.IDLE, A2)], "not the live attempt"),
        ([created(), started(), resumed()], "attempt is not interrupted"),
        (
            [
                created(),
                started(),
                interrupted(InterruptReason.CRASHED),
                interrupted(InterruptReason.CRASHED),
            ],
            "attempt is not active",
        ),
        (
            [created(), started(), TaskCompleted(occurred_at=NOW, task_id=T1)],
            "attempt is still live",
        ),
        ([created(), CANCELLED, started()], "task is cancelled"),
    ],
)
def test_events_that_cannot_follow_the_log_are_rejected(events: list[Event], reason: str) -> None:
    with pytest.raises(InvalidEventError, match=reason):
        project(log(*events))


def test_positions_must_increase() -> None:
    board = project(log(created()))
    with pytest.raises(InvalidEventError, match="seq does not advance"):
        apply(board, StoredEvent(seq=1, event=started()))


def test_ending_an_interrupted_attempt_clears_its_interruption() -> None:
    board = project(log(created(), started(), CRASH, ended(AttemptOutcome.ABANDONED)))
    attempt = board.tasks[T1].attempts[0]

    assert attempt.status is AttemptStatus.ENDED
    assert (attempt.interrupt_reason, attempt.resume_not_before) == (None, None)
    assert board.tasks[T1].status is TaskStatus.PENDING
