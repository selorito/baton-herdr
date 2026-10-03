from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from baton_herdr.budget.availability import Availability, fold_availability
from baton_herdr.budget.quota import (
    LOOKBACK,
    AgentBudget,
    BudgetSource,
    counted,
    fold_budgets,
    session_window,
    usage_since,
)
from baton_herdr.core.events import AttemptInterrupted, AttemptStarted, StoredEvent, TaskCreated
from baton_herdr.core.model import AgentKind, AttemptId, InterruptReason, TaskId
from baton_herdr.core.usage import RateLimitObservation, RateLimitWindow, UsageRecord, UsageTokens

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
TASK = TaskId("t")
H = timedelta(hours=1)


def record(agent: str, at: datetime, total: int, *, cache_read: int = 0) -> UsageRecord:
    return UsageRecord(
        agent=agent,  # type: ignore[arg-type]
        session_id="s",
        record_id=f"{agent}:{at.isoformat()}",
        at=at,
        tokens=UsageTokens(
            input=total // 2, output=total - total // 2, cache_read=cache_read, cache_write=0
        ),
    )


def stored(*events: object) -> list[StoredEvent]:
    return [StoredEvent.model_validate({"seq": n, "event": e}) for n, e in enumerate(events, 1)]


def claude_limit(at: datetime, resets: datetime | None) -> list[StoredEvent]:
    attempt = AttemptId(UUID(int=1))
    return stored(
        TaskCreated(occurred_at=at, task_id=TASK, title="t", instructions="i", workdir="/w"),
        AttemptStarted(occurred_at=at, task_id=TASK, attempt_id=attempt, agent=AgentKind.CLAUDE),
        AttemptInterrupted(
            occurred_at=at,
            task_id=TASK,
            attempt_id=attempt,
            reason=InterruptReason.RATE_LIMITED,
            resume_not_before=resets,
        ),
    )


def budgets(
    *,
    events: list[StoredEvent] | None = None,
    usage: list[UsageRecord] | None = None,
    limits: list[RateLimitObservation] | None = None,
    configured: int | None = None,
    availability: Availability | None = None,
) -> dict[AgentKind, AgentBudget]:
    return fold_budgets(
        (AgentKind.CLAUDE, AgentKind.CODEX, AgentKind.OPENCODE),
        events=events or [],
        usage=usage or [],
        rate_limits=limits or [],
        availability=availability or Availability(),
        now=NOW,
        claude_window_tokens=configured,
    )


def test_cache_reads_are_not_counted() -> None:
    assert counted(UsageTokens(input=1, output=2, cache_read=1000, cache_write=4)) == 7


def test_a_session_window_opens_with_the_first_response_outside_the_last_one() -> None:
    times = [NOW - 9 * H, NOW - 8 * H, NOW - 3 * H, NOW - 1 * H]
    # 9h ago opens [9h ago, 4h ago); 3h ago is outside it and opens the current one.
    assert session_window(times, now=NOW) == (NOW - 3 * H, NOW + 2 * H)
    assert session_window([NOW - 6 * H], now=NOW) is None  # closed, nothing since
    assert session_window([], now=NOW) is None
    # Responses before a known reset do not open windows after it.
    assert session_window(times, now=NOW, after=NOW - 2 * H) == (NOW - H, NOW + 4 * H)


def test_codex_figures_are_its_own_and_a_window_that_reset_is_unused() -> None:
    old = RateLimitObservation(
        agent="codex",
        session_id="a",
        at=NOW - 2 * H,
        windows=(RateLimitWindow(name="primary", window_minutes=300, used_percent=90),),
    )
    latest = RateLimitObservation(
        agent="codex",
        session_id="b",
        at=NOW - H,
        windows=(
            RateLimitWindow(
                name="primary",
                window_minutes=300,
                used_percent=80,
                resets_at=NOW - timedelta(minutes=10),
            ),
            RateLimitWindow(
                name="secondary", window_minutes=10080, used_percent=35, resets_at=NOW + 48 * H
            ),
        ),
    )
    codex = budgets(limits=[latest, old])[AgentKind.CODEX]

    assert codex.source is BudgetSource.REPORTED
    assert not codex.estimate
    assert codex.remaining_percent == 65
    assert [(w.name, w.used_percent, w.resets_at) for w in codex.windows] == [
        ("5h", 0.0, None),
        ("weekly", 35, NOW + 48 * H),
    ]


def test_without_reports_or_a_cap_the_budget_is_unknown() -> None:
    result = budgets(usage=[record("claude", NOW - H, 1000), record("opencode", NOW - H, 50)])
    claude, codex, opencode = result.values()
    assert [b.source for b in (claude, codex, opencode)] == [BudgetSource.UNKNOWN] * 3
    assert all(b.remaining_percent is None for b in (claude, codex, opencode))
    # Claude's window and usage are still worked out; only the cap is missing.
    assert claude.windows[0].used_tokens == 1000
    assert claude.windows[0].resets_at == NOW + 4 * H
    assert opencode.recent_tokens == 50


def test_claude_is_estimated_against_the_configured_cap() -> None:
    usage = [record("claude", NOW - 2 * H, 300, cache_read=10**6), record("claude", NOW - H, 100)]
    claude = budgets(usage=usage, configured=1000)[AgentKind.CLAUDE]

    assert claude.source is BudgetSource.CONFIGURED
    assert claude.estimate
    assert claude.remaining_percent == 60
    assert claude.windows[0].resets_at == NOW + 3 * H


def test_claude_learns_its_cap_from_the_last_session_limit() -> None:
    hit, resets = NOW - 4 * H, NOW - 2 * H  # window was [7h ago, 2h ago)
    usage = [
        record("claude", NOW - 8 * H, 5000),  # an earlier window: not counted
        record("claude", NOW - 7 * H, 600),
        record("claude", NOW - 5 * H, 400),
        record("claude", NOW - H, 250),  # the window after the reset
    ]
    claude = budgets(events=claude_limit(hit, resets), usage=usage)[AgentKind.CLAUDE]

    assert claude.source is BudgetSource.LEARNED
    assert claude.windows[0].cap_tokens == 1000
    assert claude.windows[0].used_tokens == 250
    assert claude.remaining_percent == 75


def test_a_configured_cap_wins_and_weekly_limits_teach_nothing() -> None:
    usage = [record("claude", NOW - 3 * H, 500)]
    session = claude_limit(NOW - 3 * H, NOW + H)
    assert budgets(events=session, usage=usage, configured=2000)[AgentKind.CLAUDE].source is (
        BudgetSource.CONFIGURED
    )
    weekly = claude_limit(NOW - 3 * H, NOW + 72 * H)
    assert budgets(events=weekly, usage=usage)[AgentKind.CLAUDE].source is BudgetSource.UNKNOWN
    spend = claude_limit(NOW - 3 * H, None)
    assert budgets(events=spend, usage=usage)[AgentKind.CLAUDE].source is BudgetSource.UNKNOWN


def test_a_limited_agent_says_until_when() -> None:
    events = claude_limit(NOW - H, NOW + H)
    result = budgets(events=events, availability=fold_availability(events))
    assert result[AgentKind.CLAUDE].limited_until == NOW + H
    assert not result[AgentKind.CLAUDE].available
    assert result[AgentKind.CODEX].available


def test_usage_is_read_back_far_enough_to_learn_from_the_last_limit() -> None:
    assert usage_since([], NOW) == NOW - LOOKBACK
    old = claude_limit(NOW - 40 * H, NOW - 38 * H)
    assert usage_since(old, NOW) == NOW - 43 * H
