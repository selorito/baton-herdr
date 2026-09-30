"""Turn an intent into the events that record it.

Commands are pure: they look at the current view of a task and return events to
append, in order, as one atomic batch. They exist so that invariants which span
more than one event are kept in one place. The main one: closing a task always
ends its live attempt first, so the log never contains a closed task with an
attempt that is still open.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from coban.core.events import AttemptEnded, Event, TaskCancelled, TaskCompleted, TaskFailed
from coban.core.model import AttemptOutcome

if TYPE_CHECKING:
    from datetime import datetime

    from coban.core.projection import TaskView


class CommandRejectedError(Exception):
    """The command does not make sense for the task as it is now."""


def complete_task(task: TaskView, *, at: datetime) -> list[Event]:
    """The work is done. A live attempt ends as succeeded."""
    closing = _end_live_attempt(task, AttemptOutcome.SUCCEEDED, at)
    return [*closing, TaskCompleted(occurred_at=at, task_id=task.task_id)]


def fail_task(task: TaskView, *, at: datetime, reason: str | None = None) -> list[Event]:
    """Give up on the task. A live attempt ends as failed."""
    closing = _end_live_attempt(task, AttemptOutcome.FAILED, at)
    return [*closing, TaskFailed(occurred_at=at, task_id=task.task_id, reason=reason)]


def cancel_task(task: TaskView, *, at: datetime, reason: str | None = None) -> list[Event]:
    """The operator no longer wants the task. A live attempt ends as abandoned.

    Cancelling is always possible for an open task, whatever its attempt is doing.
    """
    closing = _end_live_attempt(task, AttemptOutcome.ABANDONED, at)
    return [*closing, TaskCancelled(occurred_at=at, task_id=task.task_id, reason=reason)]


def _end_live_attempt(task: TaskView, outcome: AttemptOutcome, at: datetime) -> list[Event]:
    if task.status.is_terminal:
        msg = f"task {task.task_id} is already {task.status.value}"
        raise CommandRejectedError(msg)
    live = task.live_attempt
    if live is None:
        return []
    return [
        AttemptEnded(
            occurred_at=at, task_id=task.task_id, attempt_id=live.attempt_id, outcome=outcome
        )
    ]
