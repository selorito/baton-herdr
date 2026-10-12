"""What the operator is told about a project (ADR 0016). Pure, like scheduler.notices."""

from __future__ import annotations

from typing import TYPE_CHECKING

from baton_herdr.core.notify import Notice, NoticeKind

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from baton_herdr.core.changes import Changes
    from baton_herdr.core.projection import TaskView
    from baton_herdr.projects.plan import Plan

PLAN_READY = "📋 Plan ready"
PLAN_FAILED = "⚠ No plan"
DELIVERED = "✅ Project done"
HALTED = "⚠ Project stopped"


def plan_choices(task: TaskView) -> tuple[tuple[str, str], ...]:
    """Start and Cancel, bound to the plan task: ``plan:start:t-1a2b3c4d``."""
    return (("Start", f"plan:start:{task.task_id}"), ("Cancel", f"plan:cancel:{task.task_id}"))


def plan_ready(task: TaskView, plan: Plan) -> Notice:
    steps = tuple(f"{n}. {step.title}" for n, step in enumerate(plan.steps, start=1))
    return Notice(
        NoticeKind.PLAN_PROPOSED,
        task.task_id,
        plan.summary or "The plan has no summary.",
        title=task.request or task.title,
        details=steps,
        label=PLAN_READY,
        choices=plan_choices(task),
    )


def plan_unreadable(task: TaskView, reason: str) -> Notice:
    return Notice(
        NoticeKind.NEEDS_HUMAN,
        task.task_id,
        "The planning agent left no plan baton can read, so nothing runs. "
        "Send the request again with /new, perhaps in other words.",
        title=task.request or task.title,
        details=(reason,),
        label=PLAN_FAILED,
    )


def delivered(
    plan_task: TaskView,
    steps: Sequence[TaskView],
    *,
    changes: Changes | None,
    url: str | None,
    image: Path | None,
) -> Notice:
    details = [f"Preview: {url}"] if url else []
    if url and image is None:
        details.append("No screenshot: no browser was found, or it could not open the page.")
    details.append(f"Directory: {plan_task.workdir}")
    return Notice(
        NoticeKind.PROJECT_DELIVERED,
        steps[-1].task_id,
        f"All {len(steps)} steps are done.",
        title=plan_task.request or plan_task.title,
        details=tuple(details),
        blocks=(changes.stat(limit=10),) if changes is not None else (),
        label=DELIVERED,
        image=str(image) if image is not None else None,
    )


def halted(plan_task: TaskView | None, stopped: TaskView, dropped: int) -> Notice:
    """A step did not complete, so the steps after it were dropped."""
    return Notice(
        NoticeKind.NEEDS_HUMAN,
        stopped.task_id,
        f"{stopped.title} was {stopped.status.value}, so the {dropped} step(s) after it were "
        "cancelled. What the steps before it did is still in the directory.",
        title=(plan_task.request or plan_task.title) if plan_task else stopped.title,
        details=(f"Directory: {stopped.workdir}",),
        label=HALTED,
    )
