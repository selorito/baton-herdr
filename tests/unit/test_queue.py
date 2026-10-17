from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from baton_herdr.budget.availability import Availability
from baton_herdr.core.model import (
    AgentKind,
    AgentState,
    AttemptId,
    AttemptStatus,
    InterruptReason,
    TaskId,
    TaskStatus,
)
from baton_herdr.core.projection import AttemptView, TaskView
from baton_herdr.scheduler.queue import (
    agent_load,
    dispatch,
    full_agents,
    next_wake,
    select_tasks,
)

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
        # Active between cycles: batond restarted, or a person was asked; always looked at.
        task("busy", live=AttemptStatus.ACTIVE),
        task("done", closed=TaskStatus.COMPLETED),
    ]
    positions = {TaskId("new"): 1, TaskId("waiting"): 7, TaskId("busy"): 9, TaskId("done"): 4}

    first = select_tasks(tasks, positions=positions, available=BOTH, seen={})
    assert first == (TaskId("new"), TaskId("waiting"), TaskId("busy"))

    seen = {
        TaskId("new"): (1, BOTH),
        TaskId("waiting"): (7, frozenset({CODEX})),
        TaskId("busy"): (9, BOTH),
    }
    # "new" is unchanged; "waiting" was last run while only Codex was available.
    assert select_tasks(tasks, positions=positions, available=BOTH, seen=seen) == (
        TaskId("waiting"),
        TaskId("busy"),
    )
    assert select_tasks(
        tasks, positions={**positions, TaskId("new"): 12}, available=frozenset({CODEX}), seen=seen
    ) == (TaskId("new"), TaskId("busy"))


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


def on(
    name: str,
    agent: AgentKind,
    *,
    workdir: str = "",
    status: AttemptStatus = AttemptStatus.ACTIVE,
    state: AgentState = AgentState.WORKING,
) -> TaskView:
    live = AttemptView(
        attempt_id=AttemptId(uuid4()),
        agent=agent,
        status=status,
        agent_state=state,
    )
    return TaskView(
        task_id=TaskId(name), title=name, workdir=workdir or f"/w/{name}", attempts=(live,)
    )


def pending(name: str, workdir: str = "") -> TaskView:
    return TaskView(task_id=TaskId(name), title=name, workdir=workdir or f"/w/{name}")


def start(
    tasks: list[TaskView],
    *,
    running: tuple[str, ...] = (),
    available: frozenset[AgentKind] = BOTH,
    slots: dict[AgentKind, int] | None = None,
    max_parallel: int = 4,
) -> tuple[str, ...]:
    return dispatch(
        [t.task_id for t in tasks],
        {t.task_id: t for t in tasks},
        running=[TaskId(r) for r in running],
        available=available,
        slots=slots or {},
        default_slots=2,
        max_parallel=max_parallel,
    )


def test_a_slot_is_held_by_an_active_attempt_that_does_not_wait_for_a_person() -> None:
    tasks = [
        on("a", CLAUDE),
        on("b", CLAUDE),
        on("asking", CLAUDE, state=AgentState.BLOCKED_QUESTION),
        on("limited", CODEX, status=AttemptStatus.INTERRUPTED),
        on("c", CODEX),
    ]
    load = agent_load(tasks)
    assert load == {CLAUDE: 2, CODEX: 1}
    assert full_agents(load, {}, 2) == frozenset({CLAUDE})
    assert full_agents(load, {CLAUDE: 3, CODEX: 1}, 2) == frozenset({CODEX})


def test_new_tasks_start_while_an_available_agent_has_a_free_slot() -> None:
    queue = [pending(name) for name in ("one", "two", "three", "four", "five")]
    # Two agents with two slots each: four start at once, the fifth waits.
    assert start(queue) == ("one", "two", "three", "four")
    assert start(queue, slots={CLAUDE: 1, CODEX: 1}) == ("one", "two")
    # A limited agent's slots are not counted.
    assert start(queue, available=frozenset({CODEX})) == ("one", "two")
    # The bound on all workers holds, and running workers count against it.
    assert start(queue, max_parallel=3) == ("one", "two", "three")
    busy = [on("x", CLAUDE), on("y", CODEX), *queue]
    assert start(busy, running=("x", "y")) == ("one", "two")


def test_two_tasks_never_start_in_one_directory() -> None:
    queue = [
        pending("first", "/repo"),
        pending("same", "/repo/"),
        pending("other", "/elsewhere"),
    ]
    assert start(queue) == ("first", "other")
    # A task with a live attempt there, even one waiting for its limit, keeps it.
    waiting = on("waiting", CLAUDE, workdir="/repo", status=AttemptStatus.INTERRUPTED)
    assert start([waiting, *queue]) == ("waiting", "other")


def test_active_attempts_are_reattached_whatever_the_slots_unless_a_worker_drives_them() -> None:
    tasks = [on("a", CLAUDE), on("b", CLAUDE), on("c", CODEX), on("d", CODEX), pending("new")]
    assert start(tasks) == ("a", "b", "c", "d")
    assert start(tasks, running=("a", "b")) == ("c", "d")


def test_with_no_agent_available_tasks_are_let_through_to_say_so() -> None:
    queue = [pending("one"), pending("two"), pending("three", "/w/one")]
    assert start(queue, available=frozenset(), max_parallel=1) == ("one", "two")


def test_a_parallel_step_waits_for_what_it_needs_to_be_merged_and_for_its_worktree() -> None:
    base = TaskView(
        task_id=TaskId("base"),
        title="base",
        branch="baton/step-1",
        closed_as=TaskStatus.COMPLETED,
    )
    step = TaskView(
        task_id=TaskId("step"),
        title="step",
        needs=(TaskId("base"),),
        branch="baton/step-2",
        worktree="/wt/step-2",
    )

    def ready(*tasks: TaskView) -> tuple[TaskId, ...]:
        return select_tasks(list(tasks), positions={}, available=BOTH, seen={})

    assert ready(base, step) == ()  # completed, not merged yet
    merged = replace(base, merged=True)
    assert ready(merged, step) == (TaskId("step"),)
    assert ready(merged, replace(step, worktree=None)) == ()
    # Not completed: the step waits; the base itself runs once its own worktree is open.
    open_base = replace(base, closed_as=None, worktree="/wt/step-1")
    assert ready(open_base, step) == (TaskId("base"),)
