"""Budgets in words, one line per agent, for ``baton budget`` and Telegram's /budget."""

from __future__ import annotations

from typing import TYPE_CHECKING

from baton_herdr.budget.quota import BudgetSource

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import datetime, tzinfo

    from baton_herdr.budget.quota import AgentBudget, Window

_SOURCE = {
    BudgetSource.CONFIGURED: "estimate against [budget] claude_window_tokens",
    BudgetSource.LEARNED: "estimate; cap learned from the last session limit",
}


def budget_lines(budgets: Iterable[AgentBudget], *, now: datetime, zone: tzinfo) -> list[str]:
    return [_line(budget, now, zone) for budget in budgets]


def _line(budget: AgentBudget, now: datetime, zone: tzinfo) -> str:
    parts: list[str] = []
    if budget.limited_until is not None:
        parts.append(f"limited until {when(budget.limited_until, now, zone)}")
    if budget.remaining_percent is None:
        parts.append("budget unknown")
    elif budget.estimate:
        parts.append(f"~{budget.remaining_percent:.0f}% left ({_SOURCE[budget.source]})")
    else:
        parts.append(f"{budget.remaining_percent:.0f}% left")
    parts.extend(_window(w, budget, now, zone) for w in budget.windows)
    if not budget.windows:
        parts.append(f"{tokens(budget.recent_tokens)} tokens in the last 5h")
        if budget.source is BudgetSource.UNKNOWN:
            parts.append("no quota information")
    return f"{budget.agent.value}: " + " · ".join(parts)


def _window(window: Window, budget: AgentBudget, now: datetime, zone: tzinfo) -> str:
    if budget.source is BudgetSource.REPORTED:
        resets = f", resets {when(window.resets_at, now, zone)}" if window.resets_at else ""
        return f"{window.name} {window.used_percent or 0:.0f}% used{resets}"
    # Claude's estimated session window; its reset time is estimated too.
    used = tokens(window.used_tokens or 0)
    if window.resets_at is None:
        text = "no 5h session window open"
    else:
        resets = f", resets ~{when(window.resets_at, now, zone)}"
        if window.cap_tokens is None:
            text = f"{used} tokens in this 5h window{resets}"
        else:
            text = f"5h window {used} of ~{tokens(window.cap_tokens)} tokens{resets}"
    if budget.source is BudgetSource.UNKNOWN:
        text += " (set [budget] claude_window_tokens for an estimate)"
    return text


def when(at: datetime, now: datetime, zone: tzinfo) -> str:
    """``14:27`` today, ``Sat 23:37`` within a week, else the date."""
    local, today = at.astimezone(zone), now.astimezone(zone)
    if local.date() == today.date():
        return f"{local:%H:%M}"
    if abs((local.date() - today.date()).days) < 7:
        return f"{local:%a %H:%M}"
    return f"{local:%Y-%m-%d %H:%M}"


def tokens(count: int) -> str:
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.0f}k"
    return str(count)
