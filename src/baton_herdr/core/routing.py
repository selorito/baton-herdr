"""How an agent is started for a task: its tier, and the backend that gives the launch
profile (ADR 0020). Pure.

A backend says, for an agent, a tier and a kind of task, which model and flags to start
the agent with, and what the pane's environment needs. ``DirectBackend`` starts the agents
as installed; a gateway or a local model would be another backend, with the scheduler
unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping

    from baton_herdr.core.model import AgentKind


class Tier(StrEnum):
    PREMIUM = "premium"
    ECONOMY = "economy"


@dataclass(frozen=True, slots=True)
class LaunchProfile:
    """How to start one agent: added to its own command, never to an override."""

    backend: str  # "direct"; later e.g. "gateway", "local"
    # Turned into arguments by the adapter (``AgentAdapter.model_args``).
    model: str | None = None
    # Extra flags, passed as given.
    args: tuple[str, ...] = ()
    # For the pane the agent starts in. Empty for the direct backend; the runner refuses
    # a profile that needs one until a backend that does exists.
    env: Mapping[str, str] = field(default_factory=dict)
    # True: what the agent spends is not counted against its account's limits.
    own_budget: bool = False


@dataclass(frozen=True, slots=True)
class BackendHealth:
    up: bool
    detail: str


class ProviderBackend(Protocol):
    @property
    def name(self) -> str: ...

    def profile(self, agent: AgentKind, tier: Tier, role: str) -> LaunchProfile | None:
        """How to start ``agent`` for a task of kind ``role`` ("task", "plan", "step",
        "merge") in ``tier``; ``None`` when this backend cannot."""
        ...

    async def health(self) -> BackendHealth:
        """Whether the backend can start agents now."""
        ...
