from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from baton_herdr.budget.choice import choose_agent
from baton_herdr.budget.quota import AgentBudget, BudgetSource, Window
from baton_herdr.budget.report import budget_lines, tokens, when
from baton_herdr.core.events import BudgetNote
from baton_herdr.core.model import AgentKind

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)  # a Saturday; 15:00 in Istanbul
ZONE = ZoneInfo("Europe/Istanbul")
CLAUDE, CODEX, OPENCODE = AgentKind.CLAUDE, AgentKind.CODEX, AgentKind.OPENCODE
ORDER = (CLAUDE, CODEX, OPENCODE)


def claude(left: float | None, *, limited_until: datetime | None = None) -> AgentBudget:
    return AgentBudget(
        CLAUDE,
        BudgetSource.UNKNOWN if left is None else BudgetSource.CONFIGURED,
        left,
        limited_until=limited_until,
    )


def codex(left: float | None) -> AgentBudget:
    return AgentBudget(CODEX, BudgetSource.UNKNOWN if left is None else BudgetSource.REPORTED, left)


def choose(
    *budgets: AgentBudget, tried: tuple[AgentKind, ...] = ()
) -> tuple[AgentKind | None, str]:
    choice = choose_agent(
        ORDER[: len(budgets)],
        {b.agent: b for b in budgets},
        reserve_percent=10,
        zone=ZONE,
        tried=tried,
    )
    return choice.agent, choice.reason


def test_the_preference_order_holds_while_budgets_last() -> None:
    assert choose(claude(55), codex(90)) == (
        CLAUDE,
        "claude: first in preference order, ~55% left (estimate).",
    )


def test_unknown_budgets_are_not_low() -> None:
    assert choose(claude(None), codex(90))[0] is CLAUDE


def test_an_agent_below_the_reserve_goes_behind_the_others() -> None:
    assert choose(claude(4), codex(None)) == (
        CODEX,
        "codex: next in preference order (claude ~4% left (estimate), below the 10% reserve), "
        "budget unknown.",
    )


def test_when_everyone_is_short_the_one_with_most_left_goes() -> None:
    agent, reason = choose(claude(4), codex(7))
    assert agent is CODEX
    assert reason == (
        "codex: every agent that could take the task has less than the 10% reserve left; "
        "codex has the most (7% left)."
    )


def test_limited_and_tried_agents_are_skipped_and_said_so() -> None:
    limited = claude(80, limited_until=NOW + timedelta(hours=2))
    assert choose(limited, codex(90)) == (
        CODEX,
        "codex: next in preference order (claude limited until 2026-10-03 17:00), 90% left.",
    )
    assert choose(claude(80), codex(90), tried=(CLAUDE,))[1].startswith(
        "codex: next in preference order (claude already tried for this task)"
    )
    assert choose(limited, codex(90), tried=(CODEX,)) == (
        None,
        "No agent can take the task: claude limited until 2026-10-03 17:00; "
        "codex already tried for this task.",
    )


def test_the_choice_carries_every_agents_standing() -> None:
    choice = choose_agent(ORDER[:2], {CLAUDE: claude(55.04)}, reserve_percent=10, zone=ZONE)
    assert choice.notes == (
        BudgetNote(agent=CLAUDE, available=True, remaining_percent=55.0, estimate=True),
        BudgetNote(agent=CODEX, available=True),
    )


def test_budget_lines_say_where_each_figure_comes_from() -> None:
    reported = AgentBudget(
        CODEX,
        BudgetSource.REPORTED,
        65,
        (
            Window("5h", 0.0, None),
            Window("weekly", 35, NOW + timedelta(days=2)),
        ),
    )
    learned = AgentBudget(
        CLAUDE,
        BudgetSource.LEARNED,
        75,
        (Window("5h", 25, NOW + timedelta(hours=3), used_tokens=250_000, cap_tokens=1_000_000),),
    )
    unknown_cap = AgentBudget(
        CLAUDE,
        BudgetSource.UNKNOWN,
        None,
        (Window("5h", None, NOW + timedelta(hours=3), used_tokens=1_500),),
        limited_until=NOW + timedelta(hours=1),
    )
    opencode = AgentBudget(OPENCODE, BudgetSource.UNKNOWN, None, recent_tokens=12_345)

    assert budget_lines([reported, learned, unknown_cap, opencode], now=NOW, zone=ZONE) == [
        "codex: 65% left · 5h 0% used · weekly 35% used, resets Mon 15:00",
        "claude: ~75% left (estimate; cap learned from the last session limit)"
        " · 5h window 250k of ~1.0M tokens, resets ~18:00",
        "claude: limited until 16:00 · budget unknown"
        " · 2k tokens in this 5h window, resets ~18:00"
        " (set [budget] claude_window_tokens for an estimate)",
        "opencode: budget unknown · 12k tokens in the last 5h · no quota information",
    ]


@pytest.mark.parametrize(
    ("at", "text"),
    [
        (NOW, "15:00"),
        (NOW + timedelta(days=1), "Sun 15:00"),
        (NOW + timedelta(days=9), "2026-10-12 15:00"),
    ],
)
def test_times_are_local_and_as_short_as_clear(at: datetime, text: str) -> None:
    assert when(at, NOW, ZONE) == text


def test_token_counts_are_rounded_for_reading() -> None:
    assert [tokens(n) for n in (999, 12_345, 3_100_000)] == ["999", "12k", "3.1M"]
