"""Which agents can take work now, folded from the event log.

v1 knows one kind of budget fact: an attempt on an agent was interrupted because
the agent hit its usage limit. The agent is then unavailable until the reset time
the agent printed, or, when it printed none, for a configured cool-down. Token
accounting arrives later (roadmap step 2) and will feed the same view.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import TYPE_CHECKING

from baton_herdr.core.events import AttemptInterrupted, AttemptStarted
from baton_herdr.core.model import InterruptReason

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from datetime import datetime

    from baton_herdr.core.events import StoredEvent
    from baton_herdr.core.model import AgentKind, AttemptId

DEFAULT_COOLDOWN = timedelta(hours=1)


@dataclass(frozen=True, slots=True)
class Availability:
    limited_until: Mapping[AgentKind, datetime] = field(default_factory=dict)
    attempt_agents: Mapping[AttemptId, AgentKind] = field(default_factory=dict)

    def is_available(self, agent: AgentKind, now: datetime) -> bool:
        until = self.limited_until.get(agent)
        return until is None or until <= now

    def available(self, agents: Iterable[AgentKind], now: datetime) -> list[AgentKind]:
        """``agents`` that are available now, in the given order."""
        return [agent for agent in agents if self.is_available(agent, now)]


def fold_availability(
    events: Iterable[StoredEvent],
    *,
    cooldown: timedelta = DEFAULT_COOLDOWN,
    start: Availability | None = None,
) -> Availability:
    view = start or Availability()
    for stored in events:
        event = stored.event
        if isinstance(event, AttemptStarted):
            view = replace(
                view, attempt_agents={**view.attempt_agents, event.attempt_id: event.agent}
            )
        elif isinstance(event, AttemptInterrupted) and event.reason is InterruptReason.RATE_LIMITED:
            agent = view.attempt_agents.get(event.attempt_id)
            if agent is None:
                continue
            until = event.resume_not_before or event.occurred_at + cooldown
            # A later limit report can extend the wait, never shorten it.
            current = view.limited_until.get(agent)
            if current is None or until > current:
                view = replace(view, limited_until={**view.limited_until, agent: until})
    return view
