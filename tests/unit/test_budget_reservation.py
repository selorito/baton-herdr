"""Running attempts reserve what they are expected to spend still (ADR 0019)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import NAMESPACE_URL, uuid5

from baton_herdr.budget.choice import choose_agent, slots
from baton_herdr.budget.quota import AgentBudget, BudgetSource, Window
from baton_herdr.budget.report import budget_lines
from baton_herdr.budget.reservation import (
    DEFAULT_ATTEMPT_TOKENS,
    expected_cost,
    reserve,
    session_costs,
    tokens_per_percent,
)
from baton_herdr.core.events import (
    AttemptEnded,
    AttemptLocated,
    AttemptStarted,
    Event,
    TaskCreated,
)
from baton_herdr.core.fakes import InMemoryEventStore
from baton_herdr.core.model import AgentKind, AttemptId, AttemptOutcome, TaskId
from baton_herdr.core.projection import project
from baton_herdr.core.usage import UsageRecord, UsageTokens

if TYPE_CHECKING:
    from baton_herdr.core.projection import TaskView

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
SINCE = NOW - timedelta(hours=24)
CLAUDE, CODEX = AgentKind.CLAUDE, AgentKind.CODEX
ZONE = UTC


def record(session: str, tokens: int, *, agent: str = "claude", ago: int = 30) -> UsageRecord:
    return UsageRecord(
        agent=agent,  # type: ignore[arg-type]
        session_id=session,
        record_id=f"{session}:{tokens}:{ago}",
        at=NOW - timedelta(minutes=ago),
        tokens=UsageTokens(input=0, output=tokens, cache_read=10 * tokens, cache_write=0),
    )


def attempt(
    task: str, agent: AgentKind, *, session: str | None, ended: bool = False
) -> list[Event]:
    attempt_id = AttemptId(uuid5(NAMESPACE_URL, task))
    events: list[Event] = [
        TaskCreated(
            occurred_at=NOW,
            task_id=TaskId(task),
            title=task,
            instructions=task,
            workdir=f"/w/{task}",
        ),
        AttemptStarted(occurred_at=NOW, task_id=TaskId(task), attempt_id=attempt_id, agent=agent),
    ]
    if session is not None:
        events.append(
            AttemptLocated(
                occurred_at=NOW, task_id=TaskId(task), attempt_id=attempt_id, session_ref=session
            )
        )
    if ended:
        events.append(
            AttemptEnded(
                occurred_at=NOW,
                task_id=TaskId(task),
                attempt_id=attempt_id,
                outcome=AttemptOutcome.SUCCEEDED,
            )
        )
    return events


async def tasks(*groups: list[Event]) -> list[TaskView]:
    store = InMemoryEventStore()
    for events in groups:
        await store.append(events)
    return list(project(await store.read()).tasks.values())


def estimated(left: float, *, cap: int = 1_000_000) -> AgentBudget:
    used = 100 - left
    window = Window(
        "5h", used, NOW + timedelta(hours=2), used_tokens=int(cap * used / 100), cap_tokens=cap
    )
    return AgentBudget(CLAUDE, BudgetSource.CONFIGURED, left, (window,))


def reported(left: float, *, opened: timedelta = timedelta(hours=3)) -> AgentBudget:
    window = Window("5h", 100 - left, NOW + timedelta(hours=2), opened_at=NOW - opened)
    return AgentBudget(CODEX, BudgetSource.REPORTED, left, (window,))


async def test_too_few_finished_attempts_cost_the_measured_default() -> None:
    views = await tasks(attempt("a", CLAUDE, session="s1", ended=True))
    sessions = session_costs([record("s1", 1_000)])
    assert expected_cost(CLAUDE, "task", views, sessions) == DEFAULT_ATTEMPT_TOKENS


async def test_the_expected_cost_is_the_median_of_finished_attempts_with_records() -> None:
    views = await tasks(
        attempt("a", CLAUDE, session="s1", ended=True),
        attempt("b", CLAUDE, session="s2", ended=True),
        attempt("c", CLAUDE, session="s3", ended=True),
        attempt("d", CLAUDE, session="unrecorded", ended=True),  # says nothing about cost
        attempt("e", CODEX, session="x1", ended=True),  # another agent
    )
    usage = [record("s1", 10_000), record("s2", 30_000), record("s3", 90_000)]
    usage.append(record("x1", 5, agent="codex"))
    assert expected_cost(CLAUDE, "task", views, session_costs(usage)) == 30_000


async def test_a_new_attempt_reserves_its_whole_expected_cost_and_a_spending_one_the_rest() -> None:
    views = await tasks(
        attempt("new", CLAUDE, session=None),  # its session is not known yet
        attempt("busy", CLAUDE, session="s-busy"),
        attempt("over", CLAUDE, session="s-over"),  # spent more than expected
        attempt("done", CLAUDE, session="s-done", ended=True),  # holds nothing
    )
    usage = [record("s-busy", 15_000), record("s-over", 80_000), record("s-done", 1_000)]
    budgets = reserve(
        {CLAUDE: estimated(40, cap=1_000_000)}, tasks=views, usage=usage, usage_since=SINCE, now=NOW
    )
    claude = budgets[CLAUDE]
    assert claude.running == 3
    # 45k + 30k + 0 of 10k tokens per percent.
    assert claude.reserved_percent == 7.5
    assert claude.effective_percent == 32.5


async def test_a_reported_window_turns_tokens_into_a_share_from_what_it_has_seen() -> None:
    usage = [record("x", 20_000, agent="codex", ago=60), record("y", 20_000, agent="codex", ago=90)]
    # 40k tokens since the window opened, 20% used: 2k tokens per percent.
    assert tokens_per_percent(reported(80), usage, since=SINCE, now=NOW) == 2_000
    # Too little used to tell.
    assert tokens_per_percent(reported(97), usage, since=SINCE, now=NOW) is None
    # The window opened before the records that were read: its tokens are not all there.
    weekly = reported(50, opened=timedelta(days=3))
    assert tokens_per_percent(weekly, usage, since=SINCE, now=NOW) is None


async def test_without_a_rate_the_reservation_is_unknown_and_the_budget_unchanged() -> None:
    views = await tasks(attempt("new", CODEX, session=None))
    budgets = reserve({CODEX: reported(97)}, tasks=views, usage=[], usage_since=SINCE, now=NOW)
    assert budgets[CODEX].running == 1
    assert budgets[CODEX].reserved_percent is None
    assert budgets[CODEX].effective_percent == 97


def test_the_reserve_is_compared_with_what_is_left_after_running_attempts() -> None:
    claude = AgentBudget(CLAUDE, BudgetSource.REPORTED, 14, running=2, reserved_percent=9)
    codex = AgentBudget(CODEX, BudgetSource.REPORTED, 60)
    choice = choose_agent(
        (CLAUDE, CODEX), {CLAUDE: claude, CODEX: codex}, reserve_percent=10, zone=ZONE
    )
    assert choice.agent is CODEX
    assert choice.reason == (
        "codex: next in preference order (claude 14% left, ~5% after 2 running, below the "
        "10% reserve), 60% left."
    )
    assert choice.notes[0].reserved_percent == 9
    assert choice.notes[0].running == 2


def test_an_agent_below_the_reserve_after_reservations_runs_one_task_at_a_time() -> None:
    budgets = {
        CLAUDE: AgentBudget(CLAUDE, BudgetSource.REPORTED, 14, running=2, reserved_percent=9),
        CODEX: AgentBudget(CODEX, BudgetSource.UNKNOWN, None, running=3),
    }
    assert slots(budgets, {CLAUDE: 3}, default=2, reserve_percent=10) == {CLAUDE: 1}
    healthy = {
        CLAUDE: AgentBudget(CLAUDE, BudgetSource.REPORTED, 50, running=1, reserved_percent=5)
    }
    assert slots(healthy, {CLAUDE: 3}, default=2, reserve_percent=10) == {CLAUDE: 3}


def test_budget_lines_show_what_running_attempts_hold() -> None:
    budget = AgentBudget(CODEX, BudgetSource.REPORTED, 40, running=3, reserved_percent=12.5)
    (line,) = budget_lines([budget], now=NOW, zone=ZONE)
    assert line.startswith("codex: 40% left · ~28% after 3 running · ")
