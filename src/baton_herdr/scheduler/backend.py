"""The direct backend: agents started as installed, signed in to their own accounts
(ADR 0020)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from baton_herdr.core.routing import BackendHealth, LaunchProfile, Tier

if TYPE_CHECKING:
    from collections.abc import Mapping

    from baton_herdr.core.model import AgentKind

DIRECT = "direct"


class DirectBackend:
    """Premium is each agent's default model, or the one ``[scheduler.models]`` names for
    the kind of task. Economy is not offered yet."""

    name = DIRECT

    def __init__(self, models: Mapping[AgentKind, Mapping[str, str]] | None = None) -> None:
        self._models = models or {}

    def profile(self, agent: AgentKind, tier: Tier, role: str) -> LaunchProfile | None:
        if tier is not Tier.PREMIUM:
            return None
        return LaunchProfile(backend=DIRECT, model=self._models.get(agent, {}).get(role))

    async def health(self) -> BackendHealth:
        return BackendHealth(up=True, detail="agents start as installed")
