"""How a project moves (ADR 0016): a request becomes a plan task; a finished plan task's
plan is proposed to the operator; an approved plan becomes step tasks, each after the
one before it. The steps are ordinary tasks from then on.
"""

from __future__ import annotations

import contextlib
import uuid
from typing import TYPE_CHECKING, Protocol

from baton_herdr.core.commands import cancel_task
from baton_herdr.core.events import (
    AttemptStarted,
    PlanApproved,
    PlanProposed,
    PlanRejected,
    ProjectDelivered,
    TaskCreated,
)
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
from baton_herdr.scheduler.operator import ActionRefusedError, cancel

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from datetime import datetime
    from pathlib import Path

    from baton_herdr.core.events import Event, StoredEvent
    from baton_herdr.core.notify import Notice
    from baton_herdr.core.panes import PaneHost
    from baton_herdr.core.ports import Clock, EventStore, Workspace
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


class Show(Protocol):
    """A project's page link and picture (``deliver.Showcase``); faked in tests."""

    def url(self, workdir: Path) -> str | None: ...

    async def picture(self, url: str, workdir: Path) -> Path | None: ...


async def deliver_projects(
    store: EventStore,
    *,
    clock: Clock,
    workspace: Workspace | None = None,
    show: Show | None = None,
) -> list[Notice]:
    """Deliver every project whose steps have all completed: record it, and say what it
    changed, with its page's link and picture when it has a page."""
    from pathlib import Path  # noqa: PLC0415 - only the type is needed at module level

    events = await store.read()
    board = project(events)
    by_project: dict[str, list[TaskView]] = {}
    for task in board.tasks.values():
        if task.project is not None:
            by_project.setdefault(task.project, []).append(task)
    found: list[tuple[Event, Notice]] = []
    for project_id, tasks in by_project.items():
        steps = [t for t in tasks if t.role == "step"]
        plan_task = next((t for t in tasks if t.role == "plan"), None)
        if not steps or plan_task is None or steps[-1].delivered:
            continue
        if any(t.status is not TaskStatus.COMPLETED for t in steps):
            continue
        workdir = Path(steps[-1].workdir)
        start = _first_head(events, plan_task.task_id)
        changes = await workspace.changes(str(workdir), start) if workspace else None
        url = show.url(workdir) if show else None
        picture = await show.picture(url, workdir) if show and url else None
        event = ProjectDelivered(
            occurred_at=clock.now(),
            task_id=steps[-1].task_id,
            project=project_id,
            preview_url=url,
        )
        found.append(
            (event, notices.delivered(plan_task, steps, changes=changes, url=url, image=picture))
        )
    if found:
        await store.append([event for event, _ in found])
    return [notice for _, notice in found]


def _first_head(events: Sequence[StoredEvent], task_id: TaskId) -> str | None:
    """The commit the project started from: its plan task's first attempt's."""
    for stored in events:
        event = stored.event
        if isinstance(event, AttemptStarted) and event.task_id == task_id:
            return event.workdir_head
    return None


async def cancel_project(
    store: EventStore, host: PaneHost, *, project_id: str, by: str, clock: Clock
) -> int:
    """Drop a project: its waiting plan, and every open task of it, agents' panes closed.
    Returns how many tasks were cancelled."""
    tasks = [t for t in project(await store.read()).tasks.values() if t.project == project_id]
    if not tasks:
        msg = f"There is no project {project_id}."
        raise ProjectError(msg)
    for task in tasks:
        if task.role == "plan" and task.plan is not None and task.plan.status == "proposed":
            await reject_plan(store, task.task_id, by=by, clock=clock)
    cancelled = 0
    for task in tasks:
        if not task.status.is_terminal:
            with contextlib.suppress(ActionRefusedError):  # closed meanwhile
                await cancel(store=store, host=host, clock=clock, task_id=task.task_id, by=by)
                cancelled += 1
    return cancelled


async def halt_stopped_projects(store: EventStore, *, clock: Clock) -> list[Notice]:
    """A project whose step failed or was cancelled cannot go on: its later steps would wait
    for ever. They are cancelled, and the operator is told once."""
    board = project(await store.read())
    by_project: dict[str, list[TaskView]] = {}
    for task in board.tasks.values():
        if task.project is not None and task.role == "step":
            by_project.setdefault(task.project, []).append(task)
    found: list[Notice] = []
    events: list[Event] = []
    for steps in by_project.values():
        stopped = next(
            (t for t in steps if t.status in {TaskStatus.FAILED, TaskStatus.CANCELLED}), None
        )
        if stopped is None:
            continue
        waiting = [t for t in steps if t.status is TaskStatus.PENDING and t.live_attempt is None]
        if not waiting:
            continue
        for task in waiting:
            events.extend(
                cancel_task(task, at=clock.now(), reason=f"{stopped.title} did not complete")
            )
        plan_task = next(
            (t for t in board.tasks.values() if t.project == stopped.project and t.role == "plan"),
            None,
        )
        found.append(notices.halted(plan_task, stopped, len(waiting)))
    if events:
        await store.append(events)
    return found
