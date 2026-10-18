"""A plan whose steps run side by side (ADR 0017): each step in a worktree of its own,
merged back into the project directory when it completes, on real git repositories.

The agents are left out here, as in test_projects.py: their work is written into the
worktrees and their tasks completed by the test. The last test runs batond's loop."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from baton_herdr.adapters import ADAPTERS
from baton_herdr.core.commands import complete_task
from baton_herdr.core.config import (
    BatonSettings,
    DatabaseSettings,
    DetectorSettings,
    SchedulerSettings,
    UsageSettings,
)
from baton_herdr.core.fakes import (
    FakeRepository,
    FixedClock,
    InMemoryEventStore,
    RecordingNotifier,
)
from baton_herdr.core.model import AgentKind, TaskId, TaskStatus
from baton_herdr.core.notify import NoticeKind
from baton_herdr.core.projection import project
from baton_herdr.daemon import open_runtime, serve
from baton_herdr.ledger import open_event_store
from baton_herdr.projects.flow import (
    advance_steps,
    approve_plan,
    deliver_projects,
    propose_plans,
    start_project,
)
from baton_herdr.projects.notices import plan_ready
from baton_herdr.projects.plan import read_plan
from baton_herdr.scheduler.queue import select_tasks
from baton_herdr.telegram.bot import BotSettings, OperatorBot
from baton_herdr.workdir import GitRepository

from detector_binary import detector
from simulated_agents import SimulatedAgents

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from baton_herdr.core.projection import TaskView

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
REQUEST = "A website for my cafe"
# Menu and hours side by side; the index links both; a check at the end.
PLAN = {
    "summary": "Three plain HTML pages.",
    "steps": [
        {"title": "Menu", "instructions": "Write menu.html.", "needs": []},
        {"title": "Hours", "instructions": "Write hours.html.", "needs": []},
        {"title": "Index", "instructions": "Write index.html linking both.", "needs": [1, 2]},
        {"title": "Check", "instructions": "Check every link."},
    ],
}


def git(repo: Path | str, *args: str, check: bool = True) -> str:
    return subprocess.run(  # noqa: S603 - git with fixed arguments in a test repository
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@x", *args],  # noqa: S607
        capture_output=True,
        text=True,
        check=check,
    ).stdout


def write(path: str, name: str, text: str) -> None:
    from pathlib import Path  # noqa: PLC0415

    (Path(path) / name).write_text(text, encoding="utf-8")


def exists(path: str) -> bool:
    from pathlib import Path  # noqa: PLC0415

    return Path(path).exists()


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


async def approved(
    store: InMemoryEventStore, root: Path, clock: FixedClock, plan: Mapping[str, object]
) -> tuple[TaskView, list[TaskView]]:
    """A project whose plan task wrote ``plan`` and whose plan was approved."""
    _, plan_id, workdir = await start_project(store, root=root, request=REQUEST, clock=clock)
    write(str(workdir), ".baton/plan.json", json.dumps(plan))
    await finish(store, plan_id, clock)
    await propose_plans(store, clock=clock, read_text=read_text)
    step_ids = await approve_plan(store, plan_id, by="cli", clock=clock)
    board = project(await store.read())
    return board.tasks[plan_id], [board.tasks[s] for s in step_ids]


async def finish(store: InMemoryEventStore, task_id: TaskId, clock: FixedClock) -> None:
    task = project(await store.read()).tasks[task_id]
    await store.append(complete_task(task, at=clock.now()))


async def ready(store: InMemoryEventStore) -> tuple[TaskId, ...]:
    return select_tasks(
        list(project(await store.read()).tasks.values()),
        positions={},
        available=frozenset(),
        seen={},
    )


async def view(store: InMemoryEventStore, task_id: TaskId) -> TaskView:
    return project(await store.read()).tasks[task_id]


async def test_independent_steps_run_side_by_side_and_meet_in_the_project(tmp_path: Path) -> None:
    store, clock, repo = InMemoryEventStore(), FixedClock(NOW), GitRepository()
    plan_task, steps = await approved(store, tmp_path, clock, PLAN)
    menu, hours, index, check = steps
    main = plan_task.workdir

    assert [s.branch for s in steps] == [f"baton/step-{n}" for n in (1, 2, 3, 4)]
    assert all(s.after is None for s in steps)
    assert index.needs == (menu.task_id, hours.task_id)
    assert check.needs == (index.task_id,)
    assert menu.workdir == str(tmp_path / ".worktrees" / main.rsplit("/", 1)[-1] / "step-1")
    assert "At the same time other agents are doing step 2" in menu.instructions
    # Nothing starts before its worktree is open.
    assert await ready(store) == ()

    assert await advance_steps(store, repo, clock=clock) == []
    assert await ready(store) == (menu.task_id, hours.task_id)
    assert (await view(store, menu.task_id)).worktree == menu.workdir

    write(menu.workdir, "menu.html", "coffee\n")
    write(hours.workdir, "hours.html", "8 to 6\n")
    await finish(store, menu.task_id, clock)
    await finish(store, hours.task_id, clock)
    await advance_steps(store, repo, clock=clock)

    board = project(await store.read())
    assert board.tasks[menu.task_id].merged
    assert board.tasks[hours.task_id].merged
    assert sorted(git(main, "ls-files").split()) == ["hours.html", "menu.html"]
    # The index starts from both, in its own worktree; the merged ones are gone.
    assert await ready(store) == (index.task_id,)
    assert sorted(git(index.workdir, "ls-files").split()) == ["hours.html", "menu.html"]
    assert not exists(menu.workdir)

    write(index.workdir, "index.html", "<a href=menu.html>menu</a>\n")
    await finish(store, index.task_id, clock)
    await advance_steps(store, repo, clock=clock)
    await finish(store, check.task_id, clock)  # a check that changed nothing
    assert await deliver_projects(store, clock=clock) == []  # not merged yet
    await advance_steps(store, repo, clock=clock)
    (delivered,) = await deliver_projects(store, clock=clock)

    assert delivered.kind is NoticeKind.PROJECT_DELIVERED
    assert f"Directory: {main}" in delivered.details
    assert sorted(git(main, "ls-files").split()) == ["hours.html", "index.html", "menu.html"]
    assert git(main, "status", "--porcelain") == ""


async def test_a_conflicting_step_is_merged_by_a_merge_task(tmp_path: Path) -> None:
    store, clock, repo = InMemoryEventStore(), FixedClock(NOW), GitRepository()
    plan_task, (menu, hours, index, _) = await approved(store, tmp_path, clock, PLAN)
    main = plan_task.workdir
    await advance_steps(store, repo, clock=clock)
    # Both steps write the same file.
    write(menu.workdir, "index.html", "<h1>Menu</h1>\n")
    write(hours.workdir, "index.html", "<h1>Hours</h1>\n")
    await finish(store, menu.task_id, clock)
    await finish(store, hours.task_id, clock)

    await advance_steps(store, repo, clock=clock)

    board = project(await store.read())
    assert board.tasks[menu.task_id].merged
    assert board.tasks[hours.task_id].conflicted
    (merge,) = [t for t in board.tasks.values() if t.role == "merge"]
    assert merge.after == hours.task_id
    assert merge.workdir == main
    assert "git merge baton/step-2" in merge.instructions
    assert "It conflicts in index.html." in merge.instructions
    assert await ready(store) == (merge.task_id,)  # the index waits for the merge

    # The merge task's agent resolves it, and leaves the commit to baton.
    git(main, "merge", "baton/step-2", check=False)
    write(main, "index.html", "<h1>Menu</h1>\n<h1>Hours</h1>\n")
    await finish(store, merge.task_id, clock)
    await advance_steps(store, repo, clock=clock)

    assert (await view(store, hours.task_id)).merged
    assert git(main, "status", "--porcelain") == ""
    assert await ready(store) == (index.task_id,)


async def test_a_merge_task_that_leaves_the_conflict_stops_the_project(tmp_path: Path) -> None:
    store, clock = InMemoryEventStore(), FixedClock(NOW)
    repo = FakeRepository(conflicts={"baton/step-2": ("index.html",)})
    plan_task, (menu, hours, index, check) = await approved(store, tmp_path, clock, PLAN)
    await advance_steps(store, repo, clock=clock)
    await finish(store, menu.task_id, clock)
    await finish(store, hours.task_id, clock)
    await advance_steps(store, repo, clock=clock)
    (merge,) = [t for t in project(await store.read()).tasks.values() if t.role == "merge"]
    repo.resolving.clear()  # the agent gave up without merging
    await finish(store, merge.task_id, clock)

    (notice,) = await advance_steps(store, repo, clock=clock)

    assert notice.kind is NoticeKind.NEEDS_HUMAN
    assert "could not be merged" in notice.text
    assert "The 2 step(s) waiting for it were cancelled." in notice.text
    board = project(await store.read())
    assert board.tasks[hours.task_id].merge_failed
    assert board.tasks[index.task_id].status is TaskStatus.CANCELLED
    assert board.tasks[check.task_id].status is TaskStatus.CANCELLED
    assert await advance_steps(store, repo, clock=clock) == []  # said once
    assert plan_task.task_id in board.tasks


async def test_the_operator_sees_which_steps_run_at_once(tmp_path: Path) -> None:
    notice = plan_ready(_plan_view(), read_plan(json.dumps(PLAN)))
    assert notice.details == (
        "1. Menu (at once)",
        "2. Hours (at once)",
        "3. Index (after 1, 2)",
        "4. Check (after 3)",
    )
    assert notice.text.endswith("Steps that do not build on each other run at the same time.")

    store, clock = InMemoryEventStore(), FixedClock(NOW)
    _, plan_id, workdir = await start_project(store, root=tmp_path, request=REQUEST, clock=clock)
    write(str(workdir), ".baton/plan.json", json.dumps(PLAN))
    await finish(store, plan_id, clock)
    await propose_plans(store, clock=clock, read_text=read_text)
    bot = OperatorBot(
        store=store,
        host=SimulatedAgents(clock),
        adapters=ADAPTERS,
        clock=clock,
        settings=BotSettings(
            chat_id=1, owner_id=7, agents=(AgentKind.CLAUDE,), limit_cooldown=timedelta(hours=1)
        ),
        projects_root=tmp_path,
    )
    reply = await bot.button(f"plan:start:{plan_id}")
    assert reply == "Started. 4 steps, 2 of them at once to begin with."


def _plan_view() -> TaskView:
    from baton_herdr.core.projection import TaskView  # noqa: PLC0415

    return TaskView(task_id=TaskId("t-plan"), title="Plan", request=REQUEST)


async def test_batond_runs_a_parallel_project_to_its_delivery(tmp_path: Path) -> None:
    db = tmp_path / "baton.db"
    root = tmp_path / "projects"
    clock = FixedClock(NOW)
    store = await open_event_store(db)
    _, plan_id, workdir = await start_project(store, root=root, request=REQUEST, clock=clock)
    write(str(workdir), ".baton/plan.json", json.dumps(PLAN))
    await store.append(complete_task(project(await store.read()).tasks[plan_id], at=NOW))
    await propose_plans(store, clock=clock, read_text=read_text)
    await approve_plan(store, plan_id, by="cli", clock=clock)
    await store.close()

    settings = BatonSettings(
        database=DatabaseSettings(path=db),
        detector=DetectorSettings(binary=str(detector())),
        usage=UsageSettings(enabled=False),
        scheduler=SchedulerSettings(
            agents=("codex",),
            lean_sessions=False,
            start_timeout_seconds=5,
            turn_timeout_seconds=5,
            poll_interval_seconds=0.05,
        ),
    )
    stop = asyncio.Event()
    notifier = RecordingNotifier()
    async with open_runtime(
        settings, host=SimulatedAgents(clock), notifier=notifier, clock=clock
    ) as runtime:
        serving = asyncio.create_task(serve(runtime, stop=stop, idle_s=3600))
        async with asyncio.timeout(30):
            while NoticeKind.PROJECT_DELIVERED not in notifier.kinds:
                await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(serving, timeout=10)
        events = await runtime.store.read()

    board = project(events)
    steps = [t for t in board.tasks.values() if t.role == "step"]
    assert all(t.status is TaskStatus.COMPLETED and t.merged for t in steps)
    started = {e.event.task_id: e.seq for e in events if e.event.type == "attempt.started"}
    completed = {e.event.task_id: e.seq for e in events if e.event.type == "task.completed"}
    menu, hours, index, check = steps
    # Menu and hours side by side; the index after both; the check after the index.
    assert max(started[menu.task_id], started[hours.task_id]) < min(
        completed[menu.task_id], completed[hours.task_id]
    )
    assert started[index.task_id] > max(completed[menu.task_id], completed[hours.task_id])
    assert started[check.task_id] > completed[index.task_id]
