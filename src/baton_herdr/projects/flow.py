"""How a project moves (ADR 0016): a request becomes a plan task; a finished plan task's
plan is proposed to the operator; an approved plan becomes step tasks, each after the
one before it. The steps are ordinary tasks from then on.

A plan whose steps may run at the same time (ADR 0017) puts each step in a git worktree
of its own: ``advance_steps`` opens a step's worktree once what it needs is merged, and
merges each completed step back into the project directory, one at a time. A merge that
conflicts becomes a merge task for an agent.
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
    StepMergeConflicted,
    StepMerged,
    StepMergeFailed,
    StepWorktreeOpened,
    TaskCreated,
)
from baton_herdr.core.logging import get_logger
from baton_herdr.core.model import TaskId, TaskStatus
from baton_herdr.core.ports import RepositoryError
from baton_herdr.core.projection import project
from baton_herdr.projects import notices
from baton_herdr.projects.create import create_project_dir, new_project_id
from baton_herdr.projects.plan import (
    PLAN_FILE,
    Plan,
    PlanError,
    merge_prompt,
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
    from baton_herdr.core.ports import Clock, EventStore, Repository, Workspace
    from baton_herdr.core.projection import PlanView, TaskView

# Whoever acts for baton itself in the log.
BATON = "baton"
# Where a parallel plan's steps get their worktrees, under the projects root (ADR 0017).
WORKTREES = ".worktrees"


def worktree_path(project_dir: str, index: int) -> Path:
    """``<root>/.worktrees/<project dir>/step-<n>``: beside the project, under the root, so
    its folder trust is the project's own (ADR 0016)."""
    from pathlib import Path  # noqa: PLC0415 - only the type is needed at module level

    directory = Path(project_dir)
    return directory.parent / WORKTREES / directory.name / f"step-{index}"


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
    """Start a proposed plan: one step task per step, each after the one before; in a
    parallel plan, each after the steps it needs, in a worktree of its own (ADR 0017)."""
    task, waiting = await _waiting_plan(store, task_id)
    plan = Plan(summary=waiting.summary, steps=waiting.steps)
    now = clock.now()
    events: list[Event] = [PlanApproved(occurred_at=now, task_id=task_id, by=by)]
    step_ids: list[TaskId] = []
    for index, step in enumerate(plan.steps, start=1):
        step_id = _new_task_id()
        parallel = plan.parallel
        created = TaskCreated(
            occurred_at=now,
            task_id=step_id,
            title=step_title(index, len(plan.steps), step),
            instructions=step_prompt(task.request or task.title, plan, index),
            workdir=str(worktree_path(task.workdir, index)) if parallel else task.workdir,
            project=task.project,
            role="step",
            after=step_ids[-1] if step_ids and not parallel else None,
            needs=tuple(step_ids[n - 1] for n in plan.needs(index)) if parallel else (),
            branch=f"baton/step-{index}" if parallel else None,
        )
        events.append(created)
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
        if any(t.branch is not None and not t.merged for t in steps):
            continue  # a parallel step's work is not in the project yet (ADR 0017)
        workdir = Path(plan_task.workdir)
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
        if task.project is not None and task.role in {"step", "merge"}:
            by_project.setdefault(task.project, []).append(task)
    found: list[Notice] = []
    events: list[Event] = []
    for tasks in by_project.values():
        steps = [t for t in tasks if t.role == "step"]
        stopped = next(
            (t for t in tasks if t.status in {TaskStatus.FAILED, TaskStatus.CANCELLED}), None
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


async def advance_steps(store: EventStore, repo: Repository, *, clock: Clock) -> list[Notice]:
    """Move every parallel plan on (ADR 0017): merge its completed steps into the project
    directory, one at a time, and open a worktree for each step whose needs are merged.
    Returns what to tell the operator. A git failure is logged and tried again next time."""
    board = project(await store.read())
    by_project: dict[str, list[TaskView]] = {}
    for task in board.tasks.values():
        if task.project is not None:
            by_project.setdefault(task.project, []).append(task)
    found: list[Notice] = []
    for project_id, tasks in by_project.items():
        plan_task = next((t for t in tasks if t.role == "plan"), None)
        if plan_task is None or not any(t.branch is not None for t in tasks):
            continue
        try:
            found.extend(await _merge_steps(store, repo, clock, plan_task, tasks))
            await _open_worktrees(store, repo, clock, plan_task)
        except RepositoryError as err:
            get_logger("baton.projects").warning(
                "project repository", project=project_id, error=str(err)
            )
    return found


async def _merge_steps(
    store: EventStore, repo: Repository, clock: Clock, plan_task: TaskView, tasks: list[TaskView]
) -> list[Notice]:
    main = plan_task.workdir
    merges = [t for t in tasks if t.role == "merge"]
    if any(not m.status.is_terminal for m in merges):
        return []  # an agent is merging in the project directory; nothing else touches it
    for step in tasks:
        if step.branch is None or step.status is not TaskStatus.COMPLETED or step.merged:
            continue
        if step.merge_failed:
            continue
        if step.conflicted:
            done = next((m for m in merges if m.after == step.task_id), None)
            if done is None or done.status is not TaskStatus.COMPLETED:
                continue  # it failed or was cancelled: halt_stopped_projects says so
            notice = await _take_resolved(store, repo, clock, plan_task, step)
            if notice is not None:
                return [notice]
            continue
        await repo.commit_all(step.workdir, f"baton: {step.title}")
        await repo.commit_all(main, "baton: the project before a merge")
        result = await repo.merge(main, step.branch, f"baton: merge {step.title}")
        now = clock.now()
        if result.commit is not None:
            await store.append(
                [StepMerged(occurred_at=now, task_id=step.task_id, commit=result.commit)]
            )
            with contextlib.suppress(RepositoryError):
                await repo.remove_worktree(main, step.workdir)
            continue
        await store.append(
            [
                StepMergeConflicted(occurred_at=now, task_id=step.task_id, files=result.conflicts),
                TaskCreated(
                    occurred_at=now,
                    task_id=_new_task_id(),
                    title=f"Merge {step.title}"[:80],
                    instructions=merge_prompt(step.branch, step.title, result.conflicts),
                    workdir=main,
                    project=step.project,
                    role="merge",
                    after=step.task_id,
                ),
            ]
        )
        return []  # one merge task at a time
    return []


async def _take_resolved(
    store: EventStore, repo: Repository, clock: Clock, plan_task: TaskView, step: TaskView
) -> Notice | None:
    """After a merge task: the step is merged when its branch is in the project's history;
    otherwise the project stops, and the steps that wait are cancelled."""
    assert step.branch is not None  # noqa: S101 - only parallel steps conflict
    main = plan_task.workdir
    reason = ""
    try:
        await repo.finish_merge(main, f"baton: merge {step.title}")
        merged = await repo.contains(main, step.branch)
    except RepositoryError as err:
        merged, reason = False, str(err)
    now = clock.now()
    if merged:
        head = await repo.commit_all(main, f"baton: after merging {step.title}")
        await store.append([StepMerged(occurred_at=now, task_id=step.task_id, commit=head)])
        with contextlib.suppress(RepositoryError):
            await repo.remove_worktree(main, step.workdir)
        return None
    reason = reason or f"{step.branch} is not merged into the project"
    board = project(await store.read())
    waiting = [
        t
        for t in board.tasks.values()
        if t.project == step.project
        and t.role == "step"
        and t.status is TaskStatus.PENDING
        and t.live_attempt is None
    ]
    events: list[Event] = [
        StepMergeFailed(occurred_at=now, task_id=step.task_id, reason=reason[:500])
    ]
    for task in waiting:
        events.extend(cancel_task(task, at=now, reason=f"{step.title} could not be merged"))
    await store.append(events)
    return notices.merge_failed(plan_task, step, reason, len(waiting))


async def _open_worktrees(
    store: EventStore, repo: Repository, clock: Clock, plan_task: TaskView
) -> None:
    board = project(await store.read())
    tasks = [t for t in board.tasks.values() if t.project == plan_task.project]
    if any(t.role == "merge" and not t.status.is_terminal for t in tasks):
        return  # the project directory may be half merged
    for step in tasks:
        if step.branch is None or step.worktree is not None or step.status.is_terminal:
            continue
        needed = [board.tasks[n] for n in step.needs]
        if any(n.status is not TaskStatus.COMPLETED or not n.merged for n in needed):
            continue
        # The plan, or what was merged since, as the step's starting point.
        await repo.commit_all(plan_task.workdir, "baton: the project as planned")
        base = await repo.add_worktree(plan_task.workdir, step.workdir, step.branch)
        await store.append(
            [
                StepWorktreeOpened(
                    occurred_at=clock.now(),
                    task_id=step.task_id,
                    path=step.workdir,
                    branch=step.branch,
                    base=base,
                )
            ]
        )
