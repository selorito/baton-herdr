"""The operator sees and sets tiers from Telegram and the CLI (ADR 0020, phase 4)."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import NAMESPACE_URL, uuid5

from typer.testing import CliRunner

from baton_herdr.adapters import ADAPTERS
from baton_herdr.cli import app
from baton_herdr.core.commands import complete_task
from baton_herdr.core.events import (
    AttemptEnded,
    AttemptStarted,
    TaskEscalated,
    TaskTierChanged,
)
from baton_herdr.core.fakes import FixedClock, InMemoryEventStore, RecordingNotifier
from baton_herdr.core.model import (
    AgentKind,
    AttemptId,
    AttemptOutcome,
    InterruptReason,
    TaskId,
    TaskStatus,
)
from baton_herdr.core.projection import project
from baton_herdr.core.routing import ModelSpec, Tier
from baton_herdr.daemon import add_task
from baton_herdr.ledger import open_event_store
from baton_herdr.projects.flow import propose_plans
from baton_herdr.scheduler.runner import RunnerSettings, TaskRunner
from baton_herdr.telegram.bot import HELP, BotSettings, OperatorBot, split_tier

from detector_binary import rust_detector
from simulated_agents import SimulatedAgents

if TYPE_CHECKING:
    from pathlib import Path

NOW = datetime(2026, 10, 23, 9, 0, tzinfo=UTC)
CLOCK = FixedClock(NOW)
CLAUDE, CODEX = AgentKind.CLAUDE, AgentKind.CODEX
PLAN = {
    "summary": "A page.",
    "steps": [{"title": "Page", "instructions": "Write index.html."}],
}


def bot(store: InMemoryEventStore, root: Path | None = None) -> OperatorBot:
    return OperatorBot(
        store=store,
        host=SimulatedAgents(CLOCK),
        adapters=ADAPTERS,
        clock=CLOCK,
        settings=BotSettings(
            chat_id=1, owner_id=7, agents=(CLAUDE, CODEX), limit_cooldown=timedelta(hours=1)
        ),
        projects_root=root,
    )


def started(task_id: TaskId, name: str, tier: Tier = Tier.PREMIUM) -> AttemptStarted:
    return AttemptStarted(
        occurred_at=NOW,
        task_id=task_id,
        attempt_id=AttemptId(uuid5(NAMESPACE_URL, name)),
        agent=CLAUDE,
        tier=tier,
    )


async def test_tier_sets_what_the_next_attempt_runs_in() -> None:
    store = InMemoryEventStore()
    idle = await add_task(store, title="idle", instructions="-", workdir="/a", clock=CLOCK)
    busy = await add_task(store, title="busy", instructions="-", workdir="/b", clock=CLOCK)
    await store.append([started(busy, "busy")])

    assert "/tier <task> economy|premium" in HELP
    assert await bot(store).command(f"/tier {idle} economy") == f"{idle} runs economy."
    assert await bot(store).command(f"/tier {busy} economy") == (
        f"{busy} runs economy from its next attempt; the one under way runs on."
    )
    board = project(await store.read())
    assert (board.tasks[idle].tier, board.tasks[busy].tier) == (Tier.ECONOMY, Tier.ECONOMY)
    changed = [s.event for s in await store.read() if isinstance(s.event, TaskTierChanged)]
    assert [c.by for c in changed] == ["telegram:7", "telegram:7"]


async def test_tier_says_what_is_wrong() -> None:
    store = InMemoryEventStore()
    done = await add_task(store, title="done", instructions="-", workdir="/a", clock=CLOCK)
    await store.append(complete_task(project(await store.read()).tasks[done], at=NOW))
    usage = "Say which task and tier: /tier t-1a2b3c4d economy, or premium."
    assert await bot(store).command("/tier") == usage
    assert await bot(store).command(f"/tier {done} cheap") == usage
    assert await bot(store).command("/tier t-00000000 economy") == "There is no task t-00000000."
    assert await bot(store).command(f"/tier {done} economy") == f"{done} is already completed."


async def test_status_marks_economy_and_moved_up_tasks_and_leaves_the_rest_as_it_was() -> None:
    store = InMemoryEventStore()
    plain = await add_task(store, title="plain", instructions="-", workdir="/a", clock=CLOCK)
    cheap = await add_task(store, title="cheap", instructions="-", workdir="/b", clock=CLOCK)
    up = await add_task(store, title="up", instructions="-", workdir="/c", clock=CLOCK)
    economy_attempt = started(up, "up-1", Tier.ECONOMY)
    await store.append(
        [
            started(plain, "plain"),
            started(cheap, "cheap", Tier.ECONOMY),
            economy_attempt,
            AttemptEnded(
                occurred_at=NOW,
                task_id=up,
                attempt_id=economy_attempt.attempt_id,
                outcome=AttemptOutcome.ABANDONED,
            ),
            TaskEscalated(
                occurred_at=NOW,
                task_id=up,
                agent=CLAUDE,
                reason=InterruptReason.STALLED,
                failures=1,
            ),
        ]
    )
    lines = (await bot(store).command("/status")).splitlines()
    assert lines[:3] == [
        f"{plain}  running  claude  plain",
        f"{cheap}  running  claude (economy)  cheap",
        f"{up}  pending  claude (⬆premium)  up",
    ]


def test_a_tag_in_a_request_sets_the_projects_tier_and_nothing_else() -> None:
    assert split_tier("a site\nfor my cafe") == ("a site\nfor my cafe", None)
    assert split_tier("a site for my cafe #economy") == ("a site for my cafe", Tier.ECONOMY)
    assert split_tier("#Premium a site") == ("a site", Tier.PREMIUM)
    assert split_tier("menu at cafe#economy") == ("menu at cafe#economy", None)


async def test_a_project_tagged_economy_plans_and_builds_in_economy(tmp_path: Path) -> None:
    store = InMemoryEventStore()
    reply = await bot(store, tmp_path).command("/new A page for my cafe #economy")
    assert reply.startswith("Project p-")
    (plan_task,) = project(await store.read()).tasks.values()
    assert (plan_task.request, plan_task.tier) == ("A page for my cafe", Tier.ECONOMY)

    workdir = tmp_path / plan_task.workdir.rsplit("/", 1)[-1]
    (workdir / ".baton" / "plan.json").write_text(json.dumps(PLAN), encoding="utf-8")
    await store.append(complete_task(plan_task, at=NOW))
    await propose_plans(store, clock=CLOCK, read_text=lambda p: p.read_text(encoding="utf-8"))
    await bot(store, tmp_path).button(f"plan:start:{plan_task.task_id}")
    steps = [t for t in project(await store.read()).tasks.values() if t.role == "step"]
    assert [s.tier for s in steps] == [Tier.ECONOMY]


async def test_economy_set_by_hand_undoes_a_move_up() -> None:
    store = InMemoryEventStore()
    task_id = await add_task(
        store, title="t", instructions="-", workdir="/w", clock=CLOCK, tier=Tier.ECONOMY
    )
    attempt = started(task_id, "t-1", Tier.ECONOMY)
    await store.append(
        [
            attempt,
            AttemptEnded(
                occurred_at=NOW,
                task_id=task_id,
                attempt_id=attempt.attempt_id,
                outcome=AttemptOutcome.ABANDONED,
            ),
            TaskEscalated(
                occurred_at=NOW,
                task_id=task_id,
                agent=CLAUDE,
                reason=InterruptReason.STALLED,
                failures=1,
            ),
        ]
    )
    await bot(store).command(f"/tier {task_id} economy")
    host = SimulatedAgents(CLOCK, scripts={CLAUDE: "claude-finish.toml"})
    runner = TaskRunner(
        detector=rust_detector(),
        store=store,
        host=host,
        adapters=ADAPTERS,
        notifier=RecordingNotifier(),
        clock=CLOCK,
        settings=RunnerSettings(
            agents=(CLAUDE,),
            start_timeout_s=2,
            turn_timeout_s=2,
            poll_interval_s=0.05,
            economy={CLAUDE: ModelSpec(model="haiku")},
        ),
    )
    assert await runner.run(task_id) is TaskStatus.COMPLETED
    assert host.launched == ["claude --model haiku"]


def test_the_cli_sets_a_tier_and_status_marks_economy_attempts(tmp_path: Path) -> None:
    db = tmp_path / "baton.db"

    async def seed() -> TaskId:
        store = await open_event_store(db)
        try:
            task_id = await add_task(store, title="page", instructions="-", workdir="/w")
            await store.append([started(task_id, "cli", Tier.ECONOMY)])
            return task_id
        finally:
            await store.close()

    task_id = asyncio.run(seed())
    env = {"BATON_DATABASE__PATH": str(db)}
    cli = CliRunner()
    assert "claude/economy:active" in cli.invoke(app, ["status"], env=env).output
    result = cli.invoke(app, ["task", "tier", task_id, "premium"], env=env)
    assert result.exit_code == 0, result.output
    assert result.output.strip() == (
        f"{task_id} runs premium from its next attempt; the one under way runs on"
    )
    refused = cli.invoke(app, ["task", "tier", "t-00000000", "economy"], env=env)
    assert refused.exit_code == 1
