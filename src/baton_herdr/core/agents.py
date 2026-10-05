"""What baton needs to know about each kind of agent.

Implementations live in ``baton_herdr.adapters``, one module per agent: commands,
resume, start-up answers and permission prompts. The agents' screen rules are
baton-detect's (``crates/baton-detect/rules/``, ADR 0011).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence

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

    def permission_command(self, screen: str) -> str | None:
        """The shell command a permission prompt on ``screen`` asks to run, or ``None``.

        Read for baton's policy (ADR 0013). ``None`` for anything that is not a shell
        command, or that cannot be read with certainty; a person then decides. When in
        doubt about where the command ends, include more: extra lines make the policy
        ask, missing ones could let a command through unread.
        """
        ...

    def permission_keys(self, evidence: str, *, approve: bool) -> Sequence[str] | None:
        """Keys that approve (or deny) the permission prompt behind ``evidence``, or ``None``.

        Only for tool permission prompts whose keys were verified on a recorded
        screen; anything else is answered at the terminal (ADR 0009).
        """
        ...
