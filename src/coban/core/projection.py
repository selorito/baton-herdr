"""Current state as a pure function of the event log.

``project`` folds events into a :class:`Board`. Nothing here performs I/O, reads
the clock or keeps state between calls, so replaying the same events always
gives the same board, and a board can be advanced incrementally with ``apply``.

The fold is strict: an event that cannot follow the events before it raises
:class:`InvalidEventError`. Writers validate a command against the current board
(``apply`` on a copy) before appending, so a log that fails to replay is corrupt.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from coban.core.events import (
    AgentStateObserved,
    AttemptEnded,
    AttemptInterrupted,
    AttemptLocated,
    AttemptPrompted,
    AttemptResumed,
    AttemptStarted,
    OperatorActed,
    StoredEvent,
    TaskCancelled,
    TaskCompleted,
    TaskCreated,
    TaskFailed,
)
from coban.core.model import (
    AgentKind,
    AgentState,
    AttemptId,
    AttemptOutcome,
    AttemptStatus,
    InterruptReason,
    TaskId,
    TaskStatus,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from datetime import datetime


class InvalidEventError(Exception):
    """An event does not fit the state produced by the events before it."""

    def __init__(self, stored: StoredEvent, reason: str) -> None:
        self.stored = stored
        self.reason = reason
        super().__init__(f"event {stored.seq} ({stored.event.type}): {reason}")


@dataclass(frozen=True, slots=True)
class AttemptView:
    attempt_id: AttemptId
    agent: AgentKind
    status: AttemptStatus = AttemptStatus.ACTIVE
    agent_state: AgentState = AgentState.UNKNOWN
    pane_id: str | None = None
    session_ref: str | None = None
    interrupt_reason: InterruptReason | None = None
    resume_not_before: datetime | None = None
    outcome: AttemptOutcome | None = None


@dataclass(frozen=True, slots=True)
class TaskView:
    task_id: TaskId
    title: str
    instructions: str = ""
    workdir: str = ""
    attempts: tuple[AttemptView, ...] = ()
    # Set once, by task.completed / task.failed / task.cancelled.
    closed_as: TaskStatus | None = None

    @property
    def live_attempt(self) -> AttemptView | None:
        """The attempt that has not ended, if any. Only the newest attempt can be live."""
        if self.attempts and self.attempts[-1].status is not AttemptStatus.ENDED:
            return self.attempts[-1]
        return None

    @property
    def status(self) -> TaskStatus:
        if self.closed_as is not None:
            return self.closed_as
        live = self.live_attempt
        if live is None:
            return TaskStatus.PENDING
        if live.status is AttemptStatus.INTERRUPTED:
            return TaskStatus.WAITING
        if live.agent_state.needs_human:
            return TaskStatus.NEEDS_HUMAN
        return TaskStatus.RUNNING


@dataclass(frozen=True, slots=True)
class Board:
    """Every task and the position of the last event folded in."""

    tasks: Mapping[TaskId, TaskView] = field(default_factory=dict)
    last_seq: int = 0


def project(events: Iterable[StoredEvent], *, start: Board | None = None) -> Board:
    board = start or Board()
    for stored in events:
        board = apply(board, stored)
    return board


def apply(board: Board, stored: StoredEvent) -> Board:
    """Return the board after ``stored``; ``board`` itself is not modified."""
    if stored.seq <= board.last_seq:
        raise InvalidEventError(stored, f"seq does not advance past {board.last_seq}")
    event = stored.event

    if isinstance(event, TaskCreated):
        if event.task_id in board.tasks:
            raise InvalidEventError(stored, "task already exists")
        task = TaskView(
            task_id=event.task_id,
            title=event.title,
            instructions=event.instructions,
            workdir=event.workdir,
        )
    else:
        existing = board.tasks.get(event.task_id)
        if existing is None:
            raise InvalidEventError(stored, "unknown task")
        if existing.status.is_terminal:
            raise InvalidEventError(stored, f"task is {existing.status.value}")
        task = _apply_to_task(existing, stored)

    return Board(tasks={**board.tasks, task.task_id: task}, last_seq=stored.seq)


def _apply_to_task(task: TaskView, stored: StoredEvent) -> TaskView:
    event = stored.event
    match event:
        case TaskCompleted() | TaskFailed() | TaskCancelled():
            # A closed task never has a live attempt: the attempt's end must be
            # its own event in the log, not something a reader has to infer.
            if task.live_attempt is not None:
                raise InvalidEventError(stored, "an attempt is still live")
            return replace(task, closed_as=_CLOSED_AS[event.type])
        case AttemptStarted():
            if task.live_attempt is not None:
                raise InvalidEventError(stored, "another attempt is still live")
            if any(a.attempt_id == event.attempt_id for a in task.attempts):
                raise InvalidEventError(stored, "attempt id already used")
            attempt = AttemptView(attempt_id=event.attempt_id, agent=event.agent)
            return replace(task, attempts=(*task.attempts, attempt))
        case (
            AttemptLocated()
            | AttemptPrompted()
            | OperatorActed()
            | AgentStateObserved()
            | AttemptInterrupted()
            | AttemptResumed()
            | AttemptEnded()
        ):
            live = task.live_attempt
            if live is None or live.attempt_id != event.attempt_id:
                raise InvalidEventError(stored, "attempt is not the live attempt")
            updated = _apply_to_attempt(live, stored)
            return replace(task, attempts=(*task.attempts[:-1], updated))
        case _:
            raise InvalidEventError(stored, "event cannot be applied to an existing task")


_CLOSED_AS = {
    "task.completed": TaskStatus.COMPLETED,
    "task.failed": TaskStatus.FAILED,
    "task.cancelled": TaskStatus.CANCELLED,
}


def _apply_to_attempt(attempt: AttemptView, stored: StoredEvent) -> AttemptView:
    event = stored.event
    match event:
        case AttemptLocated():
            return replace(
                attempt,
                pane_id=event.pane_id or attempt.pane_id,
                session_ref=event.session_ref or attempt.session_ref,
            )
        case AgentStateObserved():
            return replace(attempt, agent_state=event.state)
        case AttemptPrompted() | OperatorActed():
            return attempt
        case AttemptInterrupted():
            if attempt.status is not AttemptStatus.ACTIVE:
                raise InvalidEventError(stored, "attempt is not active")
            return replace(
                attempt,
                status=AttemptStatus.INTERRUPTED,
                interrupt_reason=event.reason,
                resume_not_before=event.resume_not_before,
            )
        case AttemptResumed():
            if attempt.status is not AttemptStatus.INTERRUPTED:
                raise InvalidEventError(stored, "attempt is not interrupted")
            return replace(
                attempt,
                status=AttemptStatus.ACTIVE,
                # What the screen showed before the interruption is stale now.
                agent_state=AgentState.UNKNOWN,
                interrupt_reason=None,
                resume_not_before=None,
            )
        case AttemptEnded():
            return replace(
                attempt,
                status=AttemptStatus.ENDED,
                outcome=event.outcome,
                # An ended attempt is no longer waiting for anything.
                interrupt_reason=None,
                resume_not_before=None,
            )
        case _:
            raise InvalidEventError(stored, "event cannot be applied to an attempt")
