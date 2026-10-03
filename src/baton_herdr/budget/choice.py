"""Which agent a task's next attempt goes to, and why (ADR 0012). Pure.

The configured order is the preference. Budget only reorders it: an agent with
less than the reserve left goes behind every agent that has more or whose budget
is unknown. It never makes an agent unavailable; only a limit the agent hit does
that (``budget.availability``), so a wrong estimate costs at most one limit.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from baton_herdr.core.events import BudgetNote

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping, Sequence
    from datetime import datetime, tzinfo

    from baton_herdr.budget.quota import AgentBudget
    from baton_herdr.core.model import AgentKind


@dataclass(frozen=True, slots=True)
class Choice:
    agent: AgentKind | None
    reason: str
    notes: tuple[BudgetNote, ...]


def choose_agent(
    agents: Sequence[AgentKind],
    budgets: Mapping[AgentKind, AgentBudget],
    *,
    reserve_percent: float,
    zone: tzinfo,
    tried: Collection[AgentKind] = (),
) -> Choice:
    """Pick from ``agents`` (preference order, each with an adapter), skipping ``tried``.

    ``tried`` are the agents this run of the task already gave up on.
    """
    notes = tuple(_note(agent, budgets.get(agent)) for agent in agents)

    def low(note: BudgetNote) -> bool:
        return note.remaining_percent is not None and note.remaining_percent < reserve_percent

    def skipped(note: BudgetNote) -> str:
        if not note.available:
            until = _until(budgets.get(note.agent))
            why = f"limited until {_clock(until, zone)}" if until is not None else "limited"
        elif note.agent in tried:
            why = "already tried for this task"
        else:
            why = f"{_standing(note)}, below the {reserve_percent:g}% reserve"
        return f"{note.agent.value} {why}"

    open_ = [n for n in notes if n.available and n.agent not in tried]
    if not open_:
        reasons = "; ".join(skipped(n) for n in notes) or "none configured"
        return Choice(None, f"No agent can take the task: {reasons}.", notes)

    healthy = [n for n in open_ if not low(n)]
    if healthy:
        chosen = healthy[0]
        passed = [skipped(n) for n in notes[: notes.index(chosen)]]
        order = "first in preference order"
        if passed:
            order = f"next in preference order ({'; '.join(passed)})"
        return Choice(chosen.agent, f"{chosen.agent.value}: {order}, {_standing(chosen)}.", notes)

    # Every agent that could take it is short; the one with the most left goes first.
    chosen = max(open_, key=lambda n: n.remaining_percent or 0.0)
    reason = (
        f"{chosen.agent.value}: every agent that could take the task has less than the "
        f"{reserve_percent:g}% reserve left; {chosen.agent.value} has the most "
        f"({_standing(chosen)})."
    )
    return Choice(chosen.agent, reason, notes)


def _note(agent: AgentKind, budget: AgentBudget | None) -> BudgetNote:
    if budget is None:
        return BudgetNote(agent=agent, available=True)
    return BudgetNote(
        agent=agent,
        available=budget.available,
        remaining_percent=(
            round(budget.remaining_percent, 1) if budget.remaining_percent is not None else None
        ),
        estimate=budget.estimate,
    )


def _until(budget: AgentBudget | None) -> datetime | None:
    return budget.limited_until if budget is not None else None


def _standing(note: BudgetNote) -> str:
    if note.remaining_percent is None:
        return "budget unknown"
    text = f"{note.remaining_percent:.0f}% left"
    return f"~{text} (estimate)" if note.estimate else text


def _clock(at: datetime, zone: tzinfo) -> str:
    return f"{at.astimezone(zone):%Y-%m-%d %H:%M}"
