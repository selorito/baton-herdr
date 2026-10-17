"""Which tasks the daemon should run in a cycle, and when to look again. Pure.

A task is run again only when something that could change the outcome has
changed since the daemon last ran it: an event of its own (a new task, an
attempt the operator touched), or the set of agents that are available (a
limit has reset). Without such a change running it again would only repeat the
same notice, so it is skipped. Tasks with an active attempt are the exception:
they are re-attached every cycle.

Of those, ``dispatch`` picks the ones to start now (ADR 0017): as many as there are
free slots on the agents that are available, never two in one directory.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from baton_herdr.core.model import AttemptStatus, TaskStatus

if TYPE_CHECKING:
    from collections.abc import Collection, Iterable, Mapping, Sequence
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
    """Tasks to run now, oldest first. A task that waits for another (``after``, a
    project's next step) is not run until that one has completed (ADR 0016)."""
    by_id = {task.task_id: task for task in tasks}
    return tuple(
        task.task_id
        for task in tasks
        if not task.status.is_terminal
        and _ready(task, by_id)
        and (_active(task) or seen.get(task.task_id) != (positions.get(task.task_id, 0), available))
    )


def _ready(task: TaskView, tasks: Mapping[TaskId, TaskView]) -> bool:
    if task.after is None:
        return True
    before = tasks.get(task.after)
    return before is not None and before.status is TaskStatus.COMPLETED


def _active(task: TaskView) -> bool:
    # An active attempt that no worker drives: batond restarted under it, or it waits for
    # a person, who may answer at the terminal. Re-attaching is cheap and is the only way
    # to notice either. ``dispatch`` leaves out the ones a worker drives.
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


def agent_load(tasks: Iterable[TaskView]) -> dict[AgentKind, int]:
    """Slots in use per agent: attempts that are active and not waiting for a person."""
    load: dict[AgentKind, int] = {}
    for task in tasks:
        live = task.live_attempt
        if (
            live is not None
            and live.status is AttemptStatus.ACTIVE
            and not live.agent_state.needs_human
        ):
            load[live.agent] = load.get(live.agent, 0) + 1
    return load


def full_agents(
    load: Mapping[AgentKind, int], slots: Mapping[AgentKind, int], default_slots: int
) -> frozenset[AgentKind]:
    """Agents running as many tasks as they have slots."""
    return frozenset(agent for agent, n in load.items() if n >= slots.get(agent, default_slots))


def dispatch(  # noqa: PLR0913 - the candidates, and everything that bounds them
    candidates: Sequence[TaskId],
    tasks: Mapping[TaskId, TaskView],
    *,
    running: Collection[TaskId],
    available: Collection[AgentKind],
    slots: Mapping[AgentKind, int],
    default_slots: int,
    max_parallel: int,
) -> tuple[TaskId, ...]:
    """Of ``candidates`` (``select_tasks``), the tasks to start now, oldest first.

    ``running`` are driven by a worker already. A task whose attempt is active is always
    started: its agent is running anyway, and only a worker notices how it goes on. Every
    other task needs a free slot on an available agent and a place among
    ``max_parallel`` workers, and none starts in a directory another task is using. With
    no agent available at all, tasks are let through, to say so.
    """
    load = agent_load(tasks.values())
    free = sum(max(0, slots.get(a, default_slots) - load.get(a, 0)) for a in available)
    workers = len(running)
    busy_dirs = {
        _dir(task)
        for task in tasks.values()
        if task.task_id in running or task.live_attempt is not None
    }
    chosen: list[TaskId] = []
    for task_id in candidates:
        task = tasks[task_id]
        if task_id in running:
            continue
        if _active(task):
            chosen.append(task_id)
            continue
        own = task.live_attempt is not None  # its directory is in busy_dirs because of it
        if not own and _dir(task) in busy_dirs:
            continue
        if available and (free <= 0 or workers >= max_parallel):
            continue
        chosen.append(task_id)
        busy_dirs.add(_dir(task))
        if available:
            free -= 1
            workers += 1
    return tuple(chosen)


def _dir(task: TaskView) -> str:
    return os.path.normpath(task.workdir) if task.workdir else f"<none:{task.task_id}>"
