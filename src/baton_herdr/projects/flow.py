"""How a project moves (ADR 0016): a request becomes a plan task; a finished plan task's
plan is proposed to the operator; an approved plan becomes step tasks, each after the
one before it. The steps are ordinary tasks from then on.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from baton_herdr.core.events import PlanApproved, PlanProposed, PlanRejected, TaskCreated
from baton_herdr.core.model import TaskId, TaskStatus
from baton_herdr.core.projection import project
from baton_herdr.projects import notices
from baton_herdr.projects.create import create_project_dir, new_project_id
from baton_herdr.projects.plan import (
    PLAN_FILE,
    Plan,
    PlanError,
    plan_prompt,
    plan_title,
    read_plan,
    step_prompt,
    step_title,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime
    from pathlib import Path

    from baton_herdr.core.events import Event
    from baton_herdr.core.notify import Notice
    from baton_herdr.core.ports import Clock, EventStore
    from baton_herdr.core.projection import PlanView, TaskView

# Whoever acts for baton itself in the log.
BATON = "baton"


class ProjectError(Exception):
    """The project action does not fit the project as it is now; the message says why."""


def _new_task_id() -> TaskId:
    return TaskId(f"t-{uuid.uuid4().hex[:8]}")


async def start_project(
    store: EventStore, *, root: Path, request: str, clock: Clock
) -> tuple[str, TaskId, Path]:
    """Make the project's directory and its plan task: (project id, plan task, directory)."""
    request = request.strip()
    if not request:
        msg = "Say what to build: /new a one-page website for my cafe"
        raise ProjectError(msg)
    project_id = new_project_id()
    workdir = await create_project_dir(root, project_id, request)
    task_id = _new_task_id()
    await store.append(
        [
            TaskCreated(
                occurred_at=clock.now(),
                task_id=task_id,
                title=plan_title(request),
                instructions=plan_prompt(request),
                workdir=str(workdir),
                project=project_id,
                role="plan",
                request=request,
            )
        ]
    )
    return project_id, task_id, workdir


async def propose_plans(
    store: EventStore, *, clock: Clock, read_text: Callable[[Path], str]
) -> list[Notice]:
    """Read the plan of every plan task that has finished and has none yet; record it as
    proposed, or as rejected when it cannot be read. Returns what to tell the operator."""
    from pathlib import Path  # noqa: PLC0415 - only the type is needed at module level

    board = project(await store.read())
    found: list[tuple[Event, Notice]] = []
    for task in board.tasks.values():
        if task.role != "plan" or task.status is not TaskStatus.COMPLETED:
            continue
        if task.plan is not None or task.plan_rejected:
            continue
        now = clock.now()
        try:
            plan = read_plan(read_text(Path(task.workdir) / PLAN_FILE))
        except OSError as err:
            reason = f"{PLAN_FILE} could not be read: {err.strerror or err}"
            found.append(_unreadable(task, reason, now))
        except PlanError as err:
            found.append(_unreadable(task, str(err), now))
        else:
            event = PlanProposed(
                occurred_at=now, task_id=task.task_id, summary=plan.summary, steps=plan.steps
            )
            found.append((event, notices.plan_ready(task, plan)))
    if found:
        await store.append([event for event, _ in found])
    return [notice for _, notice in found]


def _unreadable(task: TaskView, reason: str, now: datetime) -> tuple[Event, Notice]:
    event = PlanRejected(occurred_at=now, task_id=task.task_id, by=BATON, reason=reason[:500])
    return event, notices.plan_unreadable(task, reason)


async def approve_plan(
    store: EventStore, task_id: TaskId, *, by: str, clock: Clock
) -> list[TaskId]:
    """Start a proposed plan: one step task per step, each after the one before."""
    task, waiting = await _waiting_plan(store, task_id)
    plan = Plan(summary=waiting.summary, steps=waiting.steps)
    now = clock.now()
    events: list[Event] = [PlanApproved(occurred_at=now, task_id=task_id, by=by)]
    step_ids: list[TaskId] = []
    for index, step in enumerate(plan.steps, start=1):
        step_id = _new_task_id()
        events.append(
            TaskCreated(
                occurred_at=now,
                task_id=step_id,
                title=step_title(index, len(plan.steps), step),
                instructions=step_prompt(task.request or task.title, plan, index),
                workdir=task.workdir,
                project=task.project,
                role="step",
                after=step_ids[-1] if step_ids else None,
            )
        )
        step_ids.append(step_id)
    await store.append(events)
    return step_ids


async def reject_plan(store: EventStore, task_id: TaskId, *, by: str, clock: Clock) -> None:
    """Cancel a proposed plan: nothing of it runs."""
    await _waiting_plan(store, task_id)
    await store.append(
        [PlanRejected(occurred_at=clock.now(), task_id=task_id, by=by, reason="cancelled")]
    )


async def _waiting_plan(store: EventStore, task_id: TaskId) -> tuple[TaskView, PlanView]:
    task = project(await store.read()).tasks.get(task_id)
    if task is None or task.role != "plan":
        msg = f"{task_id} is not a project's plan."
        raise ProjectError(msg)
    if task.plan is None or task.plan.status != "proposed":
        msg = "That plan is no longer waiting."
        raise ProjectError(msg)
    return task, task.plan
