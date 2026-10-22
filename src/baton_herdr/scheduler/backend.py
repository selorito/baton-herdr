"""The direct backend: agents started as installed, signed in to their own accounts
(ADR 0020)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from baton_herdr.core.routing import BackendHealth, LaunchProfile, ModelSpec, Tier

if TYPE_CHECKING:
    from collections.abc import Mapping

    from baton_herdr.core.model import AgentKind

DIRECT = "direct"


class DirectBackend:
    """Premium is each agent's default model, or the one ``[scheduler.models]`` names for
    the kind of task. Economy is the model and flags ``[tiers.economy]`` gives the agent,
    for every kind of task; an agent not named there has no economy profile."""

    name = DIRECT

    def __init__(
        self,
        models: Mapping[AgentKind, Mapping[str, str]] | None = None,
        economy: Mapping[AgentKind, ModelSpec] | None = None,
    ) -> None:
        self._models = models or {}
        self._economy = economy or {}

    def profile(self, agent: AgentKind, tier: Tier, role: str) -> LaunchProfile | None:
        if tier is Tier.PREMIUM:
            return LaunchProfile(backend=DIRECT, model=self._models.get(agent, {}).get(role))
        spec = self._economy.get(agent)
        if spec is None:
            return None
        return LaunchProfile(backend=DIRECT, model=spec.model, args=spec.args)

    async def health(self) -> BackendHealth:
        return BackendHealth(up=True, detail="agents start as installed")
