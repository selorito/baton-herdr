"""A project from Telegram (ADR 0016): /new makes a directory and a plan task; the plan
the agent wrote is proposed with Start and Cancel; Start makes the steps, run in order.

The agents are left out: the plan task's end and its plan file are written here."""

from __future__ import annotations

import asyncio
import json
import subprocess
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from baton_herdr.adapters import ADAPTERS
from baton_herdr.core.commands import complete_task
from baton_herdr.core.fakes import FixedClock, InMemoryEventStore
from baton_herdr.core.model import AgentKind, TaskId, TaskStatus
from baton_herdr.core.notify import NoticeKind
from baton_herdr.core.projection import project
from baton_herdr.projects.flow import propose_plans
from baton_herdr.scheduler.queue import select_tasks
from baton_herdr.telegram.bot import BotSettings, OperatorBot
from baton_herdr.telegram.notifier import format_notice, notice_buttons

from simulated_agents import SimulatedAgents

if TYPE_CHECKING:
    from pathlib import Path

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
PLAN = {
    "summary": "A static page with the menu, in plain HTML and CSS.",
    "steps": [
        {"title": "Page", "instructions": "Write index.html with the menu."},
        {"title": "Style", "instructions": "Add style.css and link it."},
        {"title": "Check", "instructions": "Open index.html and check every link."},
    ],
}


def bot(store: InMemoryEventStore, root: Path, clock: FixedClock) -> OperatorBot:
    return OperatorBot(
        store=store,
        host=SimulatedAgents(clock),
        adapters=ADAPTERS,
        clock=clock,
        settings=BotSettings(
            chat_id=1,
            owner_id=7,
            agents=(AgentKind.CLAUDE, AgentKind.CODEX),
            limit_cooldown=timedelta(hours=1),
        ),
        projects_root=root,
    )


async def new_project(store: InMemoryEventStore, root: Path, clock: FixedClock) -> TaskId:
    reply = await bot(store, root, clock).command("/new A one-page website for my cafe")
    assert reply.startswith("Project p-")
    (plan_task,) = project(await store.read()).tasks.values()
    return plan_task.task_id


async def finish(store: InMemoryEventStore, task_id: TaskId, clock: FixedClock) -> None:
    task = project(await store.read()).tasks[task_id]
    await store.append(complete_task(task, at=clock.now()))


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


async def test_a_request_is_planned_approved_and_run_step_after_step(tmp_path: Path) -> None:
    store, clock = InMemoryEventStore(), FixedClock(NOW)
    plan_id = await new_project(store, tmp_path, clock)

    plan_task = project(await store.read()).tasks[plan_id]
    workdir = tmp_path / plan_task.workdir.rsplit("/", 1)[-1]
    assert plan_task.role == "plan"
    assert plan_task.request == "A one-page website for my cafe"
    assert workdir.name.startswith("a-one-page-website-for-my-cafe-")
    log = await asyncio.to_thread(
        subprocess.run,
        ["git", "log", "--oneline"],
        cwd=workdir,
        capture_output=True,
        text=True,
        check=True,
    )
    assert len(log.stdout.splitlines()) == 1  # one empty commit to measure changes from

    # Nothing to propose while the plan task runs.
    assert await propose_plans(store, clock=clock, read_text=read_text) == []

    (workdir / ".baton" / "plan.json").write_text(json.dumps(PLAN), encoding="utf-8")
    await finish(store, plan_id, clock)
    (notice,) = await propose_plans(store, clock=clock, read_text=read_text)
    assert notice.kind is NoticeKind.PLAN_PROPOSED
    assert notice.urgent
    assert notice.details == ("1. Page", "2. Style", "3. Check")
    assert format_notice(notice).startswith("📋 Plan ready\n<b>A one-page website for my cafe</b>")
    assert notice_buttons(notice) == [
        ("Start", f"plan:start:{plan_id}"),
        ("Cancel", f"plan:cancel:{plan_id}"),
    ]
    assert await propose_plans(store, clock=clock, read_text=read_text) == []  # once

    started = await bot(store, tmp_path, clock).button(f"plan:start:{plan_id}")
    assert started == "Started. 3 steps, one after another."
    board = project(await store.read())
    steps = [t for t in board.tasks.values() if t.role == "step"]
    assert [t.title for t in steps] == ["1/3 Page", "2/3 Style", "3/3 Check"]
    assert [t.after for t in steps] == [None, steps[0].task_id, steps[1].task_id]
    assert all(t.workdir == plan_task.workdir and t.project == plan_task.project for t in steps)
    assert "A one-page website for my cafe" in steps[1].instructions
    assert "Step 2, Style: Add style.css and link it." in steps[1].instructions

    async def ready() -> tuple[TaskId, ...]:
        return select_tasks(
            list(project(await store.read()).tasks.values()),
            positions={},
            available=frozenset(),
            seen={},
        )

    assert await ready() == (steps[0].task_id,)
    await finish(store, steps[0].task_id, clock)
    assert await ready() == (steps[1].task_id,)

    # The plan is spent: a second press does nothing.
    again = await bot(store, tmp_path, clock).button(f"plan:start:{plan_id}")
    assert again == "That plan is no longer waiting."


async def test_a_cancelled_plan_runs_nothing(tmp_path: Path) -> None:
    store, clock = InMemoryEventStore(), FixedClock(NOW)
    plan_id = await new_project(store, tmp_path, clock)
    workdir = project(await store.read()).tasks[plan_id].workdir
    (tmp_path / workdir.rsplit("/", 1)[-1] / ".baton" / "plan.json").write_text(
        json.dumps(PLAN), encoding="utf-8"
    )
    await finish(store, plan_id, clock)
    await propose_plans(store, clock=clock, read_text=read_text)

    assert await bot(store, tmp_path, clock).button(f"plan:cancel:{plan_id}") == "Cancelled."
    board = project(await store.read())
    assert [t.role for t in board.tasks.values()] == ["plan"]
    assert board.tasks[plan_id].plan_rejected


async def test_a_plan_task_that_left_no_plan_says_so_and_runs_nothing(tmp_path: Path) -> None:
    store, clock = InMemoryEventStore(), FixedClock(NOW)
    plan_id = await new_project(store, tmp_path, clock)
    await finish(store, plan_id, clock)

    (notice,) = await propose_plans(store, clock=clock, read_text=read_text)
    assert notice.label == "⚠ No plan"
    assert notice.kind is NoticeKind.NEEDS_HUMAN
    assert "could not be read" in notice.details[0]
    board = project(await store.read())
    assert board.tasks[plan_id].plan_rejected
    assert board.tasks[plan_id].status is TaskStatus.COMPLETED
    assert await propose_plans(store, clock=clock, read_text=read_text) == []


async def test_new_needs_a_request_and_buttons_need_a_plan(tmp_path: Path) -> None:
    store, clock = InMemoryEventStore(), FixedClock(NOW)
    operator = bot(store, tmp_path, clock)
    assert (await operator.command("/new")).startswith("Say what to build")
    assert await operator.button("plan:start:t-nothing") == "t-nothing is not a project's plan."
    assert await operator.button("plan:launch:t-nothing") == "Unknown button."
    assert await store.read() == []
