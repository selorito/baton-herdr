"""Which agent a task's next attempt goes to, and why (ADR 0012). Pure.

The configured order is the preference. Budget only reorders it: an agent with
less than the reserve left goes behind every agent that has more or whose budget
is unknown. It never makes an agent unavailable; only a limit the agent hit does
that (``budget.availability``), so a wrong estimate costs at most one limit.

Busy agents, those running as many tasks as they have slots (ADR 0017), go behind
every agent with a free slot, before budget is looked at. They are not refused
either: a task that has to move on takes a busy agent rather than none.

What is left is counted after the reservations of the agent's running attempts
(ADR 0019), and an agent with less than the reserve left that way runs one task at a
time (``slots``).
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

# Sessions an agent with less than the reserve left may run at once (ADR 0019).
LOW_BUDGET_SLOTS = 1


@dataclass(frozen=True, slots=True)
class Choice:
    agent: AgentKind | None
    reason: str
    notes: tuple[BudgetNote, ...]


def choose_agent(  # noqa: PLR0913 - the agents, what is known of them, and what to skip
    agents: Sequence[AgentKind],
    budgets: Mapping[AgentKind, AgentBudget],
    *,
    reserve_percent: float,
    zone: tzinfo,
    tried: Collection[AgentKind] = (),
    full: Collection[AgentKind] = (),
) -> Choice:
    """Pick from ``agents`` (preference order, each with an adapter), skipping ``tried``.

    ``tried`` are the agents this run of the task already gave up on; ``full`` those
    with every slot taken.
    """
    notes = tuple(_note(agent, budgets.get(agent)) for agent in agents)

    def low(note: BudgetNote) -> bool:
        left = _effective(note)
        return left is not None and left < reserve_percent

    def skipped(note: BudgetNote) -> str:
        if not note.available:
            until = _until(budgets.get(note.agent))
            why = f"limited until {_clock(until, zone)}" if until is not None else "limited"
        elif note.agent in tried:
            why = "already tried for this task"
        elif note.agent in full and not low(note):
            why = "busy, every slot taken"
        else:
            why = f"{_standing(note)}, below the {reserve_percent:g}% reserve"
        return f"{note.agent.value} {why}"

    open_ = [n for n in notes if n.available and n.agent not in tried]
    if not open_:
        reasons = "; ".join(skipped(n) for n in notes) or "none configured"
        return Choice(None, f"No agent can take the task: {reasons}.", notes)

    free = [n for n in open_ if n.agent not in full]
    candidates = free or open_
    healthy = [n for n in candidates if not low(n)]
    if healthy:
        chosen = healthy[0]
        passed = [skipped(n) for n in notes[: notes.index(chosen)]]
        order = "first in preference order"
        if passed:
            order = f"next in preference order ({'; '.join(passed)})"
        return Choice(chosen.agent, f"{chosen.agent.value}: {order}, {_standing(chosen)}.", notes)

    # Every agent that could take it is short; the one with the most left goes first.
    chosen = max(candidates, key=lambda n: _effective(n) or 0.0)
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
        running=budget.running,
        reserved_percent=(
            round(budget.reserved_percent, 1) if budget.reserved_percent is not None else None
        ),
    )


def slots(
    budgets: Mapping[AgentKind, AgentBudget],
    configured: Mapping[AgentKind, int],
    *,
    default: int,
    reserve_percent: float,
) -> dict[AgentKind, int]:
    """Sessions each agent may run at once: its configured slots, or one while it has
    less than the reserve left after its running attempts (ADR 0019)."""
    result = dict(configured)
    for agent, budget in budgets.items():
        left = budget.effective_percent
        if left is not None and left < reserve_percent:
            result[agent] = min(LOW_BUDGET_SLOTS, configured.get(agent, default))
    return result


def _effective(note: BudgetNote) -> float | None:
    if note.remaining_percent is None or note.reserved_percent is None:
        return note.remaining_percent
    return max(0.0, note.remaining_percent - note.reserved_percent)


def _until(budget: AgentBudget | None) -> datetime | None:
    return budget.limited_until if budget is not None else None


def _standing(note: BudgetNote) -> str:
    if note.remaining_percent is None:
        return "budget unknown"
    text = f"{note.remaining_percent:.0f}% left"
    text = f"~{text} (estimate)" if note.estimate else text
    left = _effective(note)
    if note.reserved_percent and left is not None:
        text += f", ~{left:.0f}% after {note.running} running"
    return text


def _clock(at: datetime, zone: tzinfo) -> str:
    return f"{at.astimezone(zone):%Y-%m-%d %H:%M}"
