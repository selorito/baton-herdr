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

from baton_herdr.core.events import (
    AgentChosen,
    AgentStateObserved,
    AttemptEnded,
    AttemptInterrupted,
    AttemptLocated,
    AttemptPrompted,
    AttemptResumed,
    AttemptStarted,
    OperatorActed,
    PermissionDecided,
    PlanApproved,
    PlanProposed,
    PlanRejected,
    PlanStep,
    ProjectDelivered,
    StepMergeConflicted,
    StepMerged,
    StepMergeFailed,
    StepWorktreeOpened,
    StoredEvent,
    TaskCancelled,
    TaskCompleted,
    TaskCreated,
    TaskFailed,
)
from baton_herdr.core.model import (
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
class PlanView:
    """A project's plan, on its plan task (ADR 0016)."""

    summary: str
    steps: tuple[PlanStep, ...]
    # "proposed" until the operator approves or rejects it.
    status: str = "proposed"


@dataclass(frozen=True, slots=True)
class TaskView:
    task_id: TaskId
    title: str
    instructions: str = ""
    workdir: str = ""
    attempts: tuple[AttemptView, ...] = ()
    # Set once, by task.completed / task.failed / task.cancelled.
    closed_as: TaskStatus | None = None
    # A project's task (ADR 0016), and the task it waits for.
    project: str | None = None
    role: str | None = None
    after: TaskId | None = None
    request: str | None = None
    # On a plan task: the plan, once proposed; on a project's last step: once delivered.
    plan: PlanView | None = None
    plan_rejected: bool = False
    delivered: bool = False
    # A step of a parallel plan (ADR 0017): the steps it needs, its branch, its worktree
    # once opened, and whether its branch is merged, or conflicted on the way.
    needs: tuple[TaskId, ...] = ()
    branch: str | None = None
    worktree: str | None = None
    merged: bool = False
    conflicted: bool = False
    merge_failed: bool = False

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
        if event.after is not None and event.after not in board.tasks:
            raise InvalidEventError(stored, "waits for an unknown task")
        if any(need not in board.tasks for need in event.needs):
            raise InvalidEventError(stored, "needs an unknown task")
        task = TaskView(
            task_id=event.task_id,
            title=event.title,
            instructions=event.instructions,
            workdir=event.workdir,
            project=event.project,
            role=event.role,
            after=event.after,
            request=event.request,
            needs=event.needs,
            branch=event.branch,
        )
    else:
        existing = board.tasks.get(event.task_id)
        if existing is None:
            raise InvalidEventError(stored, "unknown task")
        if isinstance(
            event,
            PlanProposed
            | PlanApproved
            | PlanRejected
            | ProjectDelivered
            | StepWorktreeOpened
            | StepMerged
            | StepMergeConflicted
            | StepMergeFailed,
        ):
            task = _apply_note(existing, stored)
            return Board(tasks={**board.tasks, task.task_id: task}, last_seq=stored.seq)
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
        case AgentChosen():
            return task  # a scheduling decision; the attempt it leads to is its own event
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
            | PermissionDecided()
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


def _apply_note(task: TaskView, stored: StoredEvent) -> TaskView:
    """Plan and delivery notes (ADR 0016): allowed on a closed task, in their order."""
    event = stored.event
    match event:
        case PlanProposed():
            if task.role != "plan" or task.plan is not None or task.plan_rejected:
                raise InvalidEventError(stored, "not a plan task waiting for its plan")
            return replace(task, plan=PlanView(summary=event.summary, steps=event.steps))
        case PlanApproved():
            if task.plan is None or task.plan.status != "proposed":
                raise InvalidEventError(stored, "no proposed plan")
            return replace(task, plan=replace(task.plan, status="approved"))
        case PlanRejected():
            if (
                task.role != "plan"
                or task.plan_rejected
                or (task.plan is not None and task.plan.status != "proposed")
            ):
                raise InvalidEventError(stored, "no plan to reject")
            plan = replace(task.plan, status="rejected") if task.plan is not None else None
            return replace(task, plan=plan, plan_rejected=True)
        case ProjectDelivered():
            if task.status is not TaskStatus.COMPLETED or task.delivered:
                raise InvalidEventError(stored, "not a completed step to deliver from")
            return replace(task, delivered=True)
        case StepWorktreeOpened() | StepMerged() | StepMergeConflicted() | StepMergeFailed():
            return _apply_step_note(task, stored)
        case _:
            raise InvalidEventError(stored, "not a note")


def _apply_step_note(task: TaskView, stored: StoredEvent) -> TaskView:
    """A parallel step's worktree and merge (ADR 0017)."""
    event = stored.event
    match event:
        case StepWorktreeOpened():
            if task.branch is None or task.worktree is not None or task.status.is_terminal:
                raise InvalidEventError(stored, "not an open step waiting for its worktree")
            return replace(task, worktree=event.path)
        case StepMerged():
            if task.branch is None or task.status is not TaskStatus.COMPLETED or task.merged:
                raise InvalidEventError(stored, "not a completed step waiting to be merged")
            return replace(task, merged=True)
        case StepMergeConflicted():
            if (
                task.branch is None
                or task.status is not TaskStatus.COMPLETED
                or task.merged
                or task.conflicted
            ):
                raise InvalidEventError(stored, "not a completed step waiting to be merged")
            return replace(task, conflicted=True)
        case StepMergeFailed():
            if not task.conflicted or task.merged or task.merge_failed:
                raise InvalidEventError(stored, "not a conflicted step waiting to be merged")
            return replace(task, merge_failed=True)
        case _:
            raise InvalidEventError(stored, "not a note")


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
        case AttemptPrompted() | OperatorActed() | PermissionDecided():
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
