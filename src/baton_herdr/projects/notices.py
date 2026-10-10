"""What the operator is told about a project (ADR 0016). Pure, like scheduler.notices."""

from __future__ import annotations

from typing import TYPE_CHECKING

from baton_herdr.core.notify import Notice, NoticeKind

if TYPE_CHECKING:
    from baton_herdr.core.projection import TaskView
    from baton_herdr.projects.plan import Plan

PLAN_READY = "📋 Plan ready"
PLAN_FAILED = "⚠ No plan"


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
