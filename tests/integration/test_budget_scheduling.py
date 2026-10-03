"""The budget steers agent choice (ADR 0012): the scheduler, Telegram's /budget, the CLI."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from typer.testing import CliRunner

from baton_herdr.adapters import ADAPTERS
from baton_herdr.budget.load import BudgetReader
from baton_herdr.cli import app
from baton_herdr.core.events import AgentChosen, AttemptStarted, BudgetNote, TaskCreated
from baton_herdr.core.fakes import (
    FixedClock,
    InMemoryEventStore,
    InMemoryUsageStore,
    RecordingNotifier,
)
from baton_herdr.core.model import AgentKind, TaskId, TaskStatus
from baton_herdr.core.usage import RateLimitObservation, RateLimitWindow, UsageRecord, UsageTokens
from baton_herdr.ledger import open_usage_store
from baton_herdr.scheduler.runner import RunnerSettings, TaskRunner
from baton_herdr.telegram.bot import HELP, BotSettings, OperatorBot

from simulated_agents import SimulatedAgents

if TYPE_CHECKING:
    from pathlib import Path

NOW = datetime(2026, 10, 3, 9, 0, tzinfo=UTC)
TASK = TaskId("power")
CLOCK = FixedClock(NOW)
COOLDOWN = timedelta(hours=1)


def codex_report(used_percent: float) -> RateLimitObservation:
    return RateLimitObservation(
        agent="codex",
        session_id="s",
        at=NOW - timedelta(minutes=5),
        windows=(
            RateLimitWindow(
                name="primary",
                window_minutes=300,
                used_percent=used_percent,
                resets_at=NOW + timedelta(hours=2),
            ),
        ),
    )


def claude_response(total: int) -> UsageRecord:
    return UsageRecord(
        agent="claude",
        session_id="c",
        record_id=f"claude:{total}",
        at=NOW - timedelta(hours=1),
        tokens=UsageTokens(input=total, output=0, cache_read=0, cache_write=0),
    )


async def store_with_task() -> InMemoryEventStore:
    store = InMemoryEventStore()
    await store.append(
        [
            TaskCreated(
                occurred_at=NOW,
                task_id=TASK,
                title="power function",
                instructions="Add power(a, b).",
                workdir="/work/calc",
            )
        ]
    )
    return store


async def test_an_agent_short_of_budget_is_passed_over_and_the_log_says_why() -> None:
    store = await store_with_task()
    usage = InMemoryUsageStore()
    await usage.record([codex_report(96)])
    runner = TaskRunner(
        store=store,
        host=SimulatedAgents(CLOCK, scripts={AgentKind.CLAUDE: "claude-finish.toml"}),
        adapters=ADAPTERS,
        notifier=RecordingNotifier(),
        clock=CLOCK,
        settings=RunnerSettings(
            agents=(AgentKind.CODEX, AgentKind.CLAUDE),  # codex preferred
            start_timeout_s=2,
            turn_timeout_s=2,
            poll_interval_s=0.05,
        ),
        budget=BudgetReader(usage=usage, cooldown=COOLDOWN),
    )

    assert await runner.run(TASK) is TaskStatus.COMPLETED

    events = [s.event for s in await store.read()]
    (chosen,) = [e for e in events if isinstance(e, AgentChosen)]
    assert chosen.agent is AgentKind.CLAUDE
    assert chosen.reason == (
        "claude: next in preference order (codex 4% left, below the 10% reserve), budget unknown."
    )
    assert chosen.budgets == (
        BudgetNote(agent=AgentKind.CODEX, available=True, remaining_percent=4.0),
        BudgetNote(agent=AgentKind.CLAUDE, available=True),
    )
    # The decision is logged before the attempt it leads to.
    started = next(i for i, e in enumerate(events) if isinstance(e, AttemptStarted))
    assert events.index(chosen) == started - 1


async def test_telegram_budget_lists_every_agent_with_its_source() -> None:
    store = await store_with_task()
    usage = InMemoryUsageStore()
    await usage.record([codex_report(30), claude_response(400_000)])
    bot = OperatorBot(
        store=store,
        host=SimulatedAgents(CLOCK),
        adapters=ADAPTERS,
        clock=CLOCK,
        settings=BotSettings(
            chat_id=1,
            owner_id=1,
            agents=(AgentKind.CLAUDE, AgentKind.CODEX),
            limit_cooldown=COOLDOWN,
            timezone="Europe/Istanbul",
        ),
        budget=BudgetReader(usage=usage, cooldown=COOLDOWN, claude_window_tokens=1_000_000),
    )

    assert "/budget" in HELP
    assert await bot.command("/budget") == (
        "claude: ~60% left (estimate against [budget] claude_window_tokens)"
        " · 5h window 400k of ~1.0M tokens, resets ~16:00\n"
        "codex: 70% left · 5h 30% used, resets 14:00"
    )


async def test_telegram_budget_without_usage_data_says_so() -> None:
    bot = OperatorBot(
        store=await store_with_task(),
        host=SimulatedAgents(CLOCK),
        adapters=ADAPTERS,
        clock=CLOCK,
        settings=BotSettings(
            chat_id=1, owner_id=1, agents=(AgentKind.CLAUDE,), limit_cooldown=COOLDOWN
        ),
    )
    assert await bot.command("/budget") == "Budgets are not available."


def test_cli_budget_reads_the_usage_tables(tmp_path: Path) -> None:
    db = tmp_path / "baton.db"

    async def seed() -> None:
        usage = await open_usage_store(db)
        try:
            # A report from a moment ago; its window resets in two hours.
            now = datetime.now(UTC)
            report = codex_report(97).model_copy(
                update={
                    "at": now - timedelta(minutes=1),
                    "windows": (
                        RateLimitWindow(
                            name="primary",
                            window_minutes=300,
                            used_percent=97,
                            resets_at=now + timedelta(hours=2),
                        ),
                    ),
                }
            )
            await usage.record([report])
        finally:
            await usage.close()

    asyncio.run(seed())
    env = {
        "BATON_DATABASE__PATH": str(db),
        "BATON_LOGGING__LEVEL": "warning",
        "BATON_SCHEDULER__AGENTS": '["codex", "claude"]',
    }
    result = CliRunner().invoke(app, ["budget"], env=env)

    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[0].startswith("codex: 3% left · 5h 97% used, resets ")
    assert lines[1].startswith("claude: budget unknown")
    assert lines[-1] == (
        "Next task: claude: next in preference order "
        "(codex 3% left, below the 10% reserve), budget unknown."
    )
