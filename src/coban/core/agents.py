"""What coban needs to know about each kind of agent.

Implementations live in ``coban.adapters``, one module per agent. Everything
agent-specific (commands, screen rules, how herdr's rule ids map to blocked
kinds) belongs there and nowhere else.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from coban.core.detection import DetectionRequest, DetectionResult
    from coban.core.model import AgentKind


class AgentAdapter(Protocol):
    @property
    def kind(self) -> AgentKind: ...

    def launch_command(self) -> str:
        """Shell command that starts a fresh session in the current directory."""
        ...

    def resume_command(self, session_ref: str) -> str:
        """Shell command that resumes ``session_ref`` directly, without a picker."""
        ...

    def classify(self, request: DetectionRequest) -> DetectionResult:
        """Refine the host's view of a screen into coban's ``AgentState``."""
        ...
