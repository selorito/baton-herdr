"""Which tasks the daemon should run in a cycle, and when to look again. Pure.

A task is run again only when something that could change the outcome has
changed since the daemon last ran it: an event of its own (a new task, an
attempt the operator touched), or the set of agents that are available (a
limit has reset). Without such a change running it again would only repeat the
same notice, so it is skipped. Tasks with an active attempt are the exception:
they are re-attached every cycle.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from baton_herdr.core.model import AttemptStatus

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    from datetime import datetime

    from baton_herdr.budget.availability import Availability
    from baton_herdr.core.events import StoredEvent
    from baton_herdr.core.model import AgentKind, TaskId
    from baton_herdr.core.projection import TaskView

type Signature = tuple[int, frozenset[AgentKind]]


def task_positions(events: Iterable[StoredEvent]) -> dict[TaskId, int]:
    """The position of each task's newest event."""
    positions: dict[TaskId, int] = {}
    for stored in events:
        positions[stored.event.task_id] = stored.seq
    return positions


def available_agents(
    availability: Availability, agents: Sequence[AgentKind], now: datetime
) -> frozenset[AgentKind]:
    return frozenset(availability.available(agents, now))


def select_tasks(
    tasks: Sequence[TaskView],
    *,
    positions: Mapping[TaskId, int],
    available: frozenset[AgentKind],
    seen: Mapping[TaskId, Signature],
) -> tuple[TaskId, ...]:
    """Tasks to run now, oldest first."""
    return tuple(
        task.task_id
        for task in tasks
        if not task.status.is_terminal
        and (_active(task) or seen.get(task.task_id) != (positions.get(task.task_id, 0), available))
    )


def _active(task: TaskView) -> bool:
    # Tasks run one at a time, so between cycles nothing drives an active attempt:
    # batond restarted under it, or it waits for a person, who may answer at the
    # terminal. Re-attaching is cheap and is the only way to notice either.
    return task.live_attempt is not None and task.live_attempt.status is AttemptStatus.ACTIVE


def next_wake(
    availability: Availability, agents: Sequence[AgentKind], now: datetime
) -> datetime | None:
    """The next moment one of ``agents`` stops being limited, if any."""
    upcoming = [
        until
        for agent in agents
        if (until := availability.limited_until.get(agent)) is not None and until > now
    ]
    return min(upcoming, default=None)
