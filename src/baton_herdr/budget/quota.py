"""What is left of each agent's usage budget (ADR 0012). Pure.

Three sources, one per agent, and each result says which it came from:

- Codex reports its own windows (``rate_limits``, collected by ``baton-detect``):
  real data. A window whose reset time has passed counts as unused.
- Claude Code reports nothing about its limits. baton estimates its 5-hour session
  window from the recorded responses and compares the counted tokens with a cap:
  the one configured (``[budget] claude_window_tokens``), else the tokens spent in
  the window that ended with the last session limit baton saw. Always an estimate.
- OpenCode depends on its provider: unknown.

Counted tokens are ``input + cache_write + output``. Cache reads are left out: they
cost a fraction of the rest and would swamp it. The cap is counted the same way,
so the choice only has to be consistent, not exact.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import TYPE_CHECKING

from baton_herdr.core.events import AttemptInterrupted, AttemptStarted
from baton_herdr.core.model import AgentKind, InterruptReason

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from datetime import datetime

    from baton_herdr.budget.availability import Availability
    from baton_herdr.core.events import StoredEvent
    from baton_herdr.core.usage import RateLimitObservation, UsageRecord, UsageTokens

# Claude Code's session limit: a window opened by the first message, 5 hours long
# (code.claude.com/docs/en/costs). Weekly limits exist too; they are not modelled.
CLAUDE_SESSION = timedelta(hours=5)
# How far back usage is read to find the current session window.
LOOKBACK = timedelta(hours=24)


class BudgetSource(StrEnum):
    REPORTED = "reported"  # the agent's own figures (Codex rate_limits, Claude's status line)
    CONFIGURED = "configured"  # estimate against [budget] claude_window_tokens
    LEARNED = "learned"  # estimate against the tokens spent before the last limit
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Window:
    """One usage window: reported by the agent, or estimated by baton."""

    name: str
    used_percent: float | None
    resets_at: datetime | None
    used_tokens: int | None = None
    cap_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class AgentBudget:
    agent: AgentKind
    source: BudgetSource
    # Share left of the tightest window; None when unknown.
    remaining_percent: float | None
    windows: tuple[Window, ...] = ()
    # Counted tokens in the last 5 hours, whatever the source.
    recent_tokens: int = 0
    # Set while the agent is limited (it hit a limit; from the event log).
    limited_until: datetime | None = None

    @property
    def estimate(self) -> bool:
        return self.source in {BudgetSource.CONFIGURED, BudgetSource.LEARNED}

    @property
    def available(self) -> bool:
        return self.limited_until is None


def counted(tokens: UsageTokens) -> int:
    return tokens.input + tokens.cache_write + tokens.output


def usage_since(events: Iterable[StoredEvent], now: datetime) -> datetime:
    """How far back usage records are needed: the lookback, or the last Claude limit."""
    since = now - LOOKBACK
    limit = _last_claude_limit(events)
    if limit is not None:
        since = min(since, limit[1] - CLAUDE_SESSION)
    return since


def fold_budgets(  # noqa: PLR0913 - the facts and the two settings they are read with
    agents: Sequence[AgentKind],
    *,
    events: Sequence[StoredEvent],
    usage: Sequence[UsageRecord],
    rate_limits: Sequence[RateLimitObservation],
    availability: Availability,
    now: datetime,
    claude_window_tokens: int | None = None,
) -> dict[AgentKind, AgentBudget]:
    budgets: dict[AgentKind, AgentBudget] = {}
    for agent in agents:
        own = [r for r in usage if r.agent == agent.value]
        recent = sum(counted(r.tokens) for r in own if now - CLAUDE_SESSION <= r.at <= now)
        if agent is AgentKind.CODEX:
            budget = _reported(agent, rate_limits, now)
        elif agent is AgentKind.CLAUDE:
            # Claude's own figures, from its status line (ADR 0015); else the estimate.
            budget = _reported(agent, rate_limits, now)
            if budget.source is BudgetSource.UNKNOWN:
                budget = _claude(own, events, now, claude_window_tokens)
        else:
            budget = AgentBudget(agent, BudgetSource.UNKNOWN, None)
        until = availability.limited_until.get(agent)
        budgets[agent] = AgentBudget(
            agent=agent,
            source=budget.source,
            remaining_percent=budget.remaining_percent,
            windows=budget.windows,
            recent_tokens=recent,
            limited_until=until if until is not None and until > now else None,
        )
    return budgets


def _reported(
    agent: AgentKind, observations: Sequence[RateLimitObservation], now: datetime
) -> AgentBudget:
    own = [o for o in observations if o.agent == agent.value and o.at <= now]
    if not own:
        return AgentBudget(agent, BudgetSource.UNKNOWN, None)
    latest = max(own, key=lambda o: o.at)
    windows = tuple(
        Window(
            name=_window_name(w.window_minutes, w.name),
            # A window that has reset since the report is unused until the next one.
            used_percent=0.0 if w.resets_at is not None and w.resets_at <= now else w.used_percent,
            resets_at=w.resets_at if w.resets_at is not None and w.resets_at > now else None,
        )
        for w in latest.windows
    )
    used = max((w.used_percent or 0.0) for w in windows)
    return AgentBudget(agent, BudgetSource.REPORTED, _left(used), windows)


def _window_name(minutes: int, fallback: str) -> str:
    if minutes % (60 * 24 * 7) == 0:
        return "weekly" if minutes == 60 * 24 * 7 else f"{minutes // (60 * 24 * 7)}w"
    if minutes % 60 == 0:
        return f"{minutes // 60}h"
    return fallback


def _claude(
    records: Sequence[UsageRecord],
    events: Sequence[StoredEvent],
    now: datetime,
    configured: int | None,
) -> AgentBudget:
    limit = _last_claude_limit(events)
    cap, source = configured, BudgetSource.CONFIGURED
    if cap is None and limit is not None:
        limited_at, ends = limit
        spent = sum(
            counted(r.tokens) for r in records if ends - CLAUDE_SESSION <= r.at <= limited_at
        )
        cap, source = (spent, BudgetSource.LEARNED) if spent > 0 else (None, source)
    # Windows after a known reset start afresh; before it, the limit's own window.
    anchor = limit[1] if limit is not None and limit[1] <= now else None
    window = session_window([r.at for r in records], now=now, after=anchor)
    used = (
        sum(counted(r.tokens) for r in records if window[0] <= r.at <= now)
        if window is not None
        else 0
    )
    if cap is None:
        estimated = Window(
            "5h", None, window[1] if window else None, used_tokens=used, cap_tokens=None
        )
        return AgentBudget(AgentKind.CLAUDE, BudgetSource.UNKNOWN, None, (estimated,))
    percent = min(100.0, 100.0 * used / cap)
    estimated = Window(
        "5h", percent, window[1] if window else None, used_tokens=used, cap_tokens=cap
    )
    return AgentBudget(AgentKind.CLAUDE, source, _left(percent), (estimated,))


def session_window(
    times: Iterable[datetime], *, now: datetime, after: datetime | None = None
) -> tuple[datetime, datetime] | None:
    """The 5-hour session window open at ``now``, if any.

    A window opens with the first response outside the previous one, as Claude
    Code's does with the first message; responses before ``after`` are ignored.
    """
    start: datetime | None = None
    for at in sorted(times):
        if after is not None and at < after:
            continue
        if at > now:
            break
        if start is None or at >= start + CLAUDE_SESSION:
            start = at
    if start is None or start + CLAUDE_SESSION <= now:
        return None
    return start, start + CLAUDE_SESSION


def _last_claude_limit(events: Iterable[StoredEvent]) -> tuple[datetime, datetime] | None:
    """When Claude last hit its session limit, and when that window ended.

    Only a limit with a printed reset within 5 hours is a session limit; a weekly
    or spend limit says nothing about the session cap.
    """
    agents: dict[object, AgentKind] = {}
    found: tuple[datetime, datetime] | None = None
    for stored in events:
        event = stored.event
        if isinstance(event, AttemptStarted):
            agents[event.attempt_id] = event.agent
        elif (
            isinstance(event, AttemptInterrupted)
            and event.reason is InterruptReason.RATE_LIMITED
            and agents.get(event.attempt_id) is AgentKind.CLAUDE
            and event.resume_not_before is not None
            and timedelta(0) < event.resume_not_before - event.occurred_at <= CLAUDE_SESSION
        ):
            found = (event.occurred_at, event.resume_not_before)
    return found


def _left(used_percent: float) -> float:
    return max(0.0, 100.0 - used_percent)
