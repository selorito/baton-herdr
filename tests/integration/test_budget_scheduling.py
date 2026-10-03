"""The budget steers agent choice (ADR 0012)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from baton_herdr.adapters import ADAPTERS
from baton_herdr.budget.load import BudgetReader
from baton_herdr.core.events import AgentChosen, AttemptStarted, BudgetNote, TaskCreated
from baton_herdr.core.fakes import (
    FixedClock,
    InMemoryEventStore,
    InMemoryUsageStore,
    RecordingNotifier,
)
from baton_herdr.core.model import AgentKind, TaskId, TaskStatus
from baton_herdr.core.usage import RateLimitObservation, RateLimitWindow, UsageRecord, UsageTokens
from baton_herdr.scheduler.runner import RunnerSettings, TaskRunner

from simulated_agents import SimulatedAgents

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
