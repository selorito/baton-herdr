"""Who classifies screens for the runner (ADR 0007, ADR 0011).

The runner asks a :class:`Detector`. ``AdapterDetector`` is the Python detector: each
agent's adapter with its rules. ``baton_herdr.detector`` adds the Rust classifier and
the modes that run the two side by side while the Rust one takes over.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from baton_herdr.core.detection import DetectionResult

if TYPE_CHECKING:
    from collections.abc import Mapping

    from baton_herdr.core.agents import AgentAdapter
    from baton_herdr.core.detection import DetectionRequest
    from baton_herdr.core.model import AgentKind


class Detector(Protocol):
    async def classify(self, request: DetectionRequest) -> DetectionResult: ...

    async def aclose(self) -> None: ...


def classify_with(
    adapters: Mapping[AgentKind, AgentAdapter], request: DetectionRequest
) -> DetectionResult:
    """The Python detector's answer: the agent's adapter, else the host's state."""
    adapter = adapters.get(request.agent)
    if adapter is None:
        return DetectionResult(state=request.host_state, evidence="baton:no-adapter")
    return adapter.classify(request)


class AdapterDetector:
    """The Python detector, as a :class:`Detector`."""

    def __init__(self, adapters: Mapping[AgentKind, AgentAdapter]) -> None:
        self._adapters = adapters

    async def classify(self, request: DetectionRequest) -> DetectionResult:
        return classify_with(self._adapters, request)

    async def aclose(self) -> None:
        return None
