"""Read what the budget needs from the stores and fold it (ADR 0012)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from baton_herdr.budget.availability import fold_availability
from baton_herdr.budget.quota import fold_budgets, usage_since

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from baton_herdr.budget.quota import AgentBudget
    from baton_herdr.core.events import StoredEvent
    from baton_herdr.core.model import AgentKind
    from baton_herdr.core.ports import UsageStore

# Codex's longest window is a week; an older report says nothing about now.
RATE_LIMITS_LOOKBACK = timedelta(days=8)


@dataclass(frozen=True, slots=True)
class BudgetReader:
    """Budgets from the event log and, when collected, the usage tables."""

    usage: UsageStore | None
    cooldown: timedelta
    claude_window_tokens: int | None = None

    async def read(
        self, agents: Sequence[AgentKind], events: Sequence[StoredEvent], now: datetime
    ) -> dict[AgentKind, AgentBudget]:
        usage = (
            await self.usage.usage(since=usage_since(events, now)) if self.usage is not None else ()
        )
        rate_limits = (
            await self.usage.rate_limits(since=now - RATE_LIMITS_LOOKBACK)
            if self.usage is not None
            else ()
        )
        return fold_budgets(
            agents,
            events=events,
            usage=usage,
            rate_limits=rate_limits,
            availability=fold_availability(events, cooldown=self.cooldown),
            now=now,
            claude_window_tokens=self.claude_window_tokens,
        )
