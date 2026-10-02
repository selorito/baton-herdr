"""What baton needs to know about each kind of agent.

Implementations live in ``baton_herdr.adapters``, one module per agent. Everything
agent-specific (commands, screen rules, how herdr's rule ids map to blocked
kinds) belongs there and nowhere else.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence

    from baton_herdr.core.detection import DetectionRequest, DetectionResult
    from baton_herdr.core.model import AgentKind


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
        """Refine the host's view of a screen into baton's ``AgentState``."""
        ...

    def resume_failed(self, screen: str) -> bool:
        """Whether ``screen`` shows the agent refusing to reopen a session."""
        ...

    def startup_answer(self, evidence: str) -> Sequence[str] | None:
        """Keys that safely dismiss a known start-up dialog, or ``None``.

        Only for dialogs whose answer has no security or account meaning (an update
        prompt, for example). Folder trust, hook review and sign-in always go to a
        person.
        """
        ...

    def permission_summary(self, screen: str) -> str | None:
        """What a permission prompt on ``screen`` asks for, in one line, or ``None``.

        Shown with Approve / Deny, so the operator knows what they approve.
        """
        ...

    def permission_keys(self, evidence: str, *, approve: bool) -> Sequence[str] | None:
        """Keys that approve (or deny) the permission prompt behind ``evidence``, or ``None``.

        Only for tool permission prompts whose keys were verified on a recorded
        screen; anything else is answered at the terminal (ADR 0009).
        """
        ...
