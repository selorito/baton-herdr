"""What running attempts are expected to spend, held against their agent's budget
(ADR 0019). Pure.

A budget figure moves only once responses have been written. Tasks assigned in the same
second all see the same figure, and none of them has spent anything yet. Each running
attempt therefore reserves its expected cost, less what its session has spent so far: that
part is in the figure already. The figure catching up is the correction, and the
reservation goes when the attempt ends. Nothing is stored: reservations are folded from
the event log and the usage records, like the budget itself.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from statistics import median
from typing import TYPE_CHECKING

from baton_herdr.budget.quota import BudgetSource, counted
from baton_herdr.core.model import AttemptStatus
from baton_herdr.core.routing import Tier

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    from datetime import datetime

    from baton_herdr.budget.quota import AgentBudget
    from baton_herdr.core.model import AgentKind
    from baton_herdr.core.projection import TaskView
    from baton_herdr.core.usage import UsageRecord

# The cost of an attempt when too few of its kind have finished: the mean of the 19 Claude
# sessions measured on 2026-10-18 (docs/devlog/2026-10-18-token-usage.md), ~43k counted
# tokens each.
DEFAULT_ATTEMPT_TOKENS = 45_000
# The finished attempts of one agent and kind whose median is the expected cost.
HISTORY = 20
MIN_HISTORY = 3
# Below this share used, tokens per percent is too noisy to turn tokens into a share.
MIN_USED_PERCENT = 5.0


@dataclass(frozen=True, slots=True)
class SessionCost:
    """The counted tokens of one session, and when it last spent."""

    tokens: int
    last_at: datetime


def reserve(
    budgets: Mapping[AgentKind, AgentBudget],
    *,
    tasks: Iterable[TaskView],
    usage: Sequence[UsageRecord],
    usage_since: datetime,
    now: datetime,
) -> dict[AgentKind, AgentBudget]:
    """``budgets`` with the reservations of every running attempt.

    ``usage`` holds the records from ``usage_since`` on: a window that opened earlier
    cannot be measured from them, and gets no reservation.
    """
    sessions = session_costs(usage)
    tasks = list(tasks)
    result: dict[AgentKind, AgentBudget] = {}
    for agent, budget in budgets.items():
        running = [
            (task, live)
            for task in tasks
            if (live := task.live_attempt) is not None
            and live.agent is agent
            and live.status is AttemptStatus.ACTIVE
        ]
        if not running:
            result[agent] = budget
            continue
        tokens = sum(
            max(
                0,
                expected_cost(agent, _role(task), tasks, sessions, tier=live.tier)
                - _spent(live.session_ref, sessions),
            )
            for task, live in running
        )
        rate = tokens_per_percent(budget, usage, since=usage_since, now=now)
        reserved = None if rate is None else round(tokens / rate, 1)
        result[agent] = replace(budget, running=len(running), reserved_percent=reserved)
    return result


def expected_cost(
    agent: AgentKind,
    role: str,
    tasks: Iterable[TaskView],
    sessions: Mapping[str, SessionCost],
    *,
    tier: Tier = Tier.PREMIUM,
) -> int:
    """The median counted tokens of the agent's last finished attempts of this kind, in
    this tier (ADR 0020): an economy model's attempts cost differently.

    An attempt with no records (its session not known, or not collected) says nothing
    about cost and is left out, rather than counted as free.
    """
    costs = sorted(
        (
            sessions[a.session_ref]
            for task in tasks
            if _role(task) == role
            for a in task.attempts
            if a.agent is agent
            and a.tier is tier
            and a.status is AttemptStatus.ENDED
            and a.session_ref is not None
            and a.session_ref in sessions
        ),
        key=lambda s: s.last_at,
    )[-HISTORY:]
    if len(costs) < MIN_HISTORY:
        return DEFAULT_ATTEMPT_TOKENS
    return int(median(s.tokens for s in costs))


def tokens_per_percent(
    budget: AgentBudget, usage: Sequence[UsageRecord], *, since: datetime, now: datetime
) -> float | None:
    """How many counted tokens one percent of the agent's tightest window is.

    For an estimate, from its cap. For a reported budget, from the tokens recorded since
    the tightest window opened and the share it reports used; ``None`` while too little
    is used, or when the window opened before the records read.
    """
    if budget.source in {BudgetSource.CONFIGURED, BudgetSource.LEARNED}:
        caps = [w.cap_tokens for w in budget.windows if w.cap_tokens]
        return caps[0] / 100 if caps else None
    if budget.source is not BudgetSource.REPORTED or not budget.windows:
        return None
    tightest = max(budget.windows, key=lambda w: w.used_percent or 0.0)
    used = tightest.used_percent or 0.0
    if used < MIN_USED_PERCENT or tightest.opened_at is None or tightest.opened_at < since:
        return None
    tokens = sum(
        counted(r.tokens)
        for r in usage
        if r.agent == budget.agent.value and tightest.opened_at <= r.at <= now
    )
    return tokens / used if tokens > 0 else None


def session_costs(usage: Iterable[UsageRecord]) -> dict[str, SessionCost]:
    sessions: dict[str, SessionCost] = {}
    for record in usage:
        known = sessions.get(record.session_id)
        sessions[record.session_id] = SessionCost(
            tokens=(known.tokens if known else 0) + counted(record.tokens),
            last_at=max(known.last_at, record.at) if known else record.at,
        )
    return sessions


def _spent(session_ref: str | None, sessions: Mapping[str, SessionCost]) -> int:
    known = sessions.get(session_ref) if session_ref is not None else None
    return known.tokens if known else 0


def _role(task: TaskView) -> str:
    return task.role or "task"
