"""Projects (ADR 0016): names, plans, prompts, and how the board and the queue treat them."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from baton_herdr.core.contract import one_line
from baton_herdr.core.events import (
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
    TaskCompleted,
    TaskCreated,
)
from baton_herdr.core.model import TaskId, TaskStatus
from baton_herdr.core.projection import InvalidEventError, project
from baton_herdr.projects.create import slug
from baton_herdr.projects.plan import (
    MAX_STEPS,
    Plan,
    PlanError,
    plan_prompt,
    read_plan,
    step_prompt,
)
from baton_herdr.scheduler.queue import select_tasks

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
PLAN = TaskId("t-plan")
STEPS = (
    PlanStep(title="Page", instructions="Write index.html with the menu."),
    PlanStep(title="Style", instructions="Add style.css."),
)


@pytest.mark.parametrize(
    ("request_text", "expected"),
    [
        ("A one-page website for my cafe", "a-one-page-website-for-my-cafe"),
        ("Kafem için tek sayfalık bir web sitesi yap", "kafem-icin-tek-sayfalik-bir-web"),
        ("!!!", "project"),
        ("../../etc/passwd", "etc-passwd"),  # never a path
    ],
)
def test_a_project_directory_is_named_from_the_requests_first_words(
    request_text: str, expected: str
) -> None:
    assert slug(request_text) == expected
    assert len(slug(request_text)) <= 32


def test_a_plan_is_read_and_checked() -> None:
    plan = read_plan(
        json.dumps({"summary": "A static page.", "steps": [s.model_dump() for s in STEPS]})
    )
    assert plan.summary == "A static page."
    assert [s.title for s in plan.steps] == ["Page", "Style"]

    with pytest.raises(PlanError, match="not JSON"):
        read_plan("the plan is: ...")
    with pytest.raises(PlanError, match="steps"):
        read_plan(json.dumps({"summary": "x", "steps": []}))
    too_many = [{"title": f"s{n}", "instructions": "x"} for n in range(MAX_STEPS + 1)]
    with pytest.raises(PlanError, match="steps"):
        read_plan(json.dumps({"summary": "x", "steps": too_many}))
    with pytest.raises(PlanError, match="title"):
        read_plan(json.dumps({"steps": [{"instructions": "x"}]}))


def test_prompts_are_one_line_and_carry_what_the_agent_has_not_seen() -> None:
    request = "A website for my cafe,\nwith the menu"
    planning = plan_prompt(request)
    assert planning == one_line(planning)
    assert "A website for my cafe, with the menu" in planning
    assert ".baton/plan.json" in planning

    plan = Plan(summary="A static page.", steps=STEPS)
    second = step_prompt(request, plan, 2)
    assert second == one_line(second)
    assert "The plan: A static page." in second
    assert "1. Page; 2. Style" in second
    assert "Step 1 is done." in second
    assert second.endswith("Step 2, Style: Add style.css.")
    assert "is done" not in step_prompt(request, plan, 1)


def stored(*events: object) -> list[StoredEvent]:
    return [StoredEvent.model_validate({"seq": n, "event": e}) for n, e in enumerate(events, 1)]


def plan_task(**fields: object) -> TaskCreated:
    return TaskCreated(
        occurred_at=NOW,
        task_id=PLAN,
        title="Plan: cafe",
        instructions="plan it",
        workdir="/p",
        project="p-1",
        role="plan",
        request="cafe",
        **fields,  # type: ignore[arg-type]
    )


def test_plan_notes_follow_the_plan_task_after_it_completed() -> None:
    proposed = PlanProposed(occurred_at=NOW, task_id=PLAN, summary="s", steps=STEPS)
    board = project(
        stored(
            plan_task(),
            TaskCompleted(occurred_at=NOW, task_id=PLAN),
            proposed,
            PlanApproved(occurred_at=NOW, task_id=PLAN, by="cli"),
        )
    )
    task = board.tasks[PLAN]
    assert task.status is TaskStatus.COMPLETED
    assert task.plan is not None
    assert task.plan.status == "approved"
    assert task.request == "cafe"

    with pytest.raises(InvalidEventError, match="no proposed plan"):
        project(stored(plan_task(), PlanApproved(occurred_at=NOW, task_id=PLAN, by="cli")))
    with pytest.raises(InvalidEventError, match="waiting for its plan"):
        project(stored(plan_task(), proposed, proposed))
    with pytest.raises(InvalidEventError, match="no plan to reject"):
        project(
            stored(
                plan_task(),
                proposed,
                PlanApproved(occurred_at=NOW, task_id=PLAN, by="cli"),
                PlanRejected(occurred_at=NOW, task_id=PLAN, by="cli"),
            )
        )
    with pytest.raises(InvalidEventError, match="not a completed step"):
        project(stored(plan_task(), ProjectDelivered(occurred_at=NOW, task_id=PLAN, project="p-1")))


def test_a_task_waits_for_the_one_it_comes_after() -> None:
    first = TaskCreated(
        occurred_at=NOW, task_id=TaskId("t-1"), title="1", instructions="x", workdir="/p"
    )
    second = TaskCreated(
        occurred_at=NOW,
        task_id=TaskId("t-2"),
        title="2",
        instructions="x",
        workdir="/p",
        after=TaskId("t-1"),
    )
    tasks = list(project(stored(first, second)).tasks.values())
    ready = select_tasks(tasks, positions={}, available=frozenset(), seen={})
    assert ready == (TaskId("t-1"),)

    tasks = list(
        project(
            stored(first, second, TaskCompleted(occurred_at=NOW, task_id=TaskId("t-1")))
        ).tasks.values()
    )
    assert select_tasks(tasks, positions={}, available=frozenset(), seen={}) == (TaskId("t-2"),)

    with pytest.raises(InvalidEventError, match="unknown task"):
        project(stored(second))


def parallel_plan() -> Plan:
    # 1 and 2 side by side; 3 builds on both; 4 on 3.
    return read_plan(
        json.dumps(
            {
                "summary": "A cafe site.",
                "steps": [
                    {"title": "Menu", "instructions": "Write menu.html.", "needs": []},
                    {"title": "Hours", "instructions": "Write hours.html.", "needs": []},
                    {"title": "Index", "instructions": "Link both.", "needs": [1, 2]},
                    {"title": "Check", "instructions": "Check every link."},
                ],
            }
        )
    )


def test_steps_that_do_not_build_on_each_other_run_alongside() -> None:
    plan = parallel_plan()
    assert plan.parallel
    assert [plan.needs(n) for n in (1, 2, 3, 4)] == [(), (), (1, 2), (3,)]
    assert plan.before(4) == {1, 2, 3}
    assert plan.alongside(1) == (2,)
    assert plan.alongside(3) == ()


def test_a_plan_without_needs_runs_in_order_as_before() -> None:
    plan = Plan(summary="", steps=STEPS)
    assert not plan.parallel
    assert plan.needs(2) == (1,)
    # Naming the step before is the same as naming nothing.
    chain = Plan(summary="", steps=(STEPS[0], STEPS[1].model_copy(update={"needs": (1,)})))
    assert not chain.parallel


def test_a_step_may_only_need_earlier_steps() -> None:
    text = json.dumps(
        {
            "steps": [
                {"title": "A", "instructions": "a", "needs": [2]},
                {"title": "B", "instructions": "b"},
            ]
        }
    )
    with pytest.raises(PlanError, match="may only need earlier steps"):
        read_plan(text)


def test_a_parallel_step_is_told_what_is_done_and_who_works_beside_it() -> None:
    plan = parallel_plan()
    first = step_prompt("A cafe site", plan, 1)
    assert "At the same time other agents are doing step 2" in first
    assert "change only what this step needs" in first
    assert "is done" not in first
    third = step_prompt("A cafe site", plan, 3)
    assert "Steps 1 and 2 are done, and the work is in this directory." in third
    assert "At the same time" not in third


def test_the_planner_is_asked_for_steps_that_can_run_side_by_side() -> None:
    prompt = plan_prompt("A cafe site")
    assert '"needs"' in prompt
    assert "at the same time" in prompt


def test_a_parallel_step_is_opened_and_merged_once_in_that_order() -> None:
    base = [
        TaskCreated(occurred_at=NOW, task_id=PLAN, title="plan", instructions="p", workdir="/p"),
        TaskCreated(
            occurred_at=NOW,
            task_id=TaskId("t-1"),
            title="1/2 Menu",
            instructions="m",
            workdir="/wt/step-1",
            branch="baton/step-1",
        ),
    ]
    opened = StepWorktreeOpened(
        occurred_at=NOW, task_id=TaskId("t-1"), path="/wt/step-1", branch="baton/step-1", base="c1"
    )
    merged = StepMerged(occurred_at=NOW, task_id=TaskId("t-1"), commit="c2")
    done = TaskCompleted(occurred_at=NOW, task_id=TaskId("t-1"))

    board = project(stored(*base, opened, done, merged))
    step = board.tasks[TaskId("t-1")]
    assert (step.worktree, step.merged) == ("/wt/step-1", True)

    with pytest.raises(InvalidEventError, match="waiting to be merged"):
        project(stored(*base, opened, merged))  # not completed
    with pytest.raises(InvalidEventError, match="waiting to be merged"):
        project(stored(*base, opened, done, merged, merged))
    with pytest.raises(InvalidEventError, match="waiting for its worktree"):
        project(stored(*base, opened, opened))
    plan_open = StepWorktreeOpened(occurred_at=NOW, task_id=PLAN, path="/x", branch="b", base="c1")
    with pytest.raises(InvalidEventError, match="waiting for its worktree"):
        project(stored(*base, plan_open))  # the plan task has no branch
    failed = StepMergeFailed(occurred_at=NOW, task_id=TaskId("t-1"), reason="no")
    with pytest.raises(InvalidEventError, match="conflicted step"):
        project(stored(*base, opened, done, failed))  # it never conflicted
    conflicted = StepMergeConflicted(occurred_at=NOW, task_id=TaskId("t-1"), files=("a",))
    assert (
        project(stored(*base, opened, done, conflicted, failed)).tasks[TaskId("t-1")].merge_failed
    )


def test_a_step_may_only_need_tasks_that_exist() -> None:
    with pytest.raises(InvalidEventError, match="needs an unknown task"):
        project(
            stored(
                TaskCreated(
                    occurred_at=NOW,
                    task_id=TaskId("t-2"),
                    title="2",
                    instructions="x",
                    workdir="/w",
                    needs=(TaskId("t-1"),),
                )
            )
        )
