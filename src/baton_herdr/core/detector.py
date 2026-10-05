"""Who classifies screens for the runner (ADR 0011).

The runner asks a :class:`Detector`. batond's is the Rust classifier,
``baton-detect classify`` (``baton_herdr.detector``). While that is down,
:class:`HostDetector` stands in, and makes no decision that needs the rules.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from baton_herdr.core.detection import DetectionResult
from baton_herdr.core.model import AgentState

if TYPE_CHECKING:
    from baton_herdr.core.detection import DetectionRequest


class Detector(Protocol):
    async def classify(self, request: DetectionRequest) -> DetectionResult: ...

    async def aclose(self) -> None: ...


# Evidence for an answer given while baton-detect gives none.
UNAVAILABLE_EVIDENCE = "baton:detector-unavailable"


class HostDetector:
    """What batond falls back to while baton-detect gives no answer: herdr's state, made safe.

    herdr's working and blocked pass through: the agent works, or a person is asked. Its
    idle does not: whether an idle screen is a finished turn, a usage limit or a question
    is what the classifier's rules tell, so without them idle reads as unknown. A task
    then neither starts nor finishes, nor moves to another agent, while the classifier
    is down; it waits, and its timeouts send it to a person in the end.
    """

    async def classify(self, request: DetectionRequest) -> DetectionResult:
        if not request.agent_running:
            return DetectionResult(
                state=request.host_state, evidence=request.host_evidence or "host:no-agent"
            )
        if request.host_state is AgentState.IDLE:
            return DetectionResult(state=AgentState.UNKNOWN, evidence=UNAVAILABLE_EVIDENCE)
        return DetectionResult(
            state=request.host_state, evidence=request.host_evidence or UNAVAILABLE_EVIDENCE
        )

    async def aclose(self) -> None:
        return None
