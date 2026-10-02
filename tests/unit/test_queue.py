from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from coban.budget.availability import Availability
from coban.core.model import (
    AgentKind,
    AttemptId,
    AttemptStatus,
    InterruptReason,
    TaskId,
    TaskStatus,
)
from coban.core.projection import AttemptView, TaskView
from coban.scheduler.queue import next_wake, select_tasks

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
CLAUDE, CODEX = AgentKind.CLAUDE, AgentKind.CODEX
BOTH = frozenset({CLAUDE, CODEX})


def attempt(status: AttemptStatus) -> AttemptView:
    reason = InterruptReason.RATE_LIMITED if status is AttemptStatus.INTERRUPTED else None
    return AttemptView(
        attempt_id=AttemptId(UUID(int=1)), agent=CLAUDE, status=status, interrupt_reason=reason
    )


def task(
    name: str, *, live: AttemptStatus | None = None, closed: TaskStatus | None = None
) -> TaskView:
    attempts = () if live is None else (attempt(live),)
    return TaskView(task_id=TaskId(name), title=name, attempts=attempts, closed_as=closed)


def test_open_tasks_run_unless_nothing_changed_since_their_last_run() -> None:
    tasks = [
        task("new"),
        task("waiting", live=AttemptStatus.INTERRUPTED),
        task("busy", live=AttemptStatus.ACTIVE),  # being driven, or waiting for a person
        task("done", closed=TaskStatus.COMPLETED),
    ]
    positions = {TaskId("new"): 1, TaskId("waiting"): 7, TaskId("busy"): 9, TaskId("done"): 4}

    first = select_tasks(tasks, positions=positions, available=BOTH, seen={})
    assert first == (TaskId("new"), TaskId("waiting"))

    seen = {TaskId("new"): (1, BOTH), TaskId("waiting"): (7, frozenset({CODEX}))}
    # "new" is unchanged; "waiting" was last run while only Codex was available.
    assert select_tasks(tasks, positions=positions, available=BOTH, seen=seen) == (
        TaskId("waiting"),
    )
    assert select_tasks(
        tasks, positions={**positions, TaskId("new"): 12}, available=frozenset({CODEX}), seen=seen
    ) == (TaskId("new"),)


def test_next_wake_is_the_earliest_future_reset_of_a_configured_agent() -> None:
    availability = Availability(
        limited_until={
            CLAUDE: NOW + timedelta(minutes=40),
            CODEX: NOW + timedelta(minutes=10),
            AgentKind.OPENCODE: NOW + timedelta(minutes=1),  # not configured
        }
    )
    assert next_wake(availability, [CLAUDE, CODEX], NOW) == NOW + timedelta(minutes=10)
    assert next_wake(availability, [CLAUDE], NOW + timedelta(hours=1)) is None
