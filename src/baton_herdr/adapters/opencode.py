"""OpenCode adapter. Evidence: fixtures/opencode/, docs/research/agents.md.

Its screen rules are baton-detect's (crates/baton-detect/rules/opencode.toml). With
herdr's OpenCode plugin installed, herdr's working / blocked / idle come from the plugin.
"""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

from baton_herdr.core.model import AgentKind

if TYPE_CHECKING:
    from collections.abc import Sequence


class OpenCodeAdapter:
    kind = AgentKind.OPENCODE

    def launch_command(self) -> str:
        return "opencode"

    def resume_command(self, session_ref: str) -> str:
        return f"opencode --session {shlex.quote(session_ref)}"

    def resume_failed(self, screen: str) -> bool:
        # The wording of a failed resume has not been observed for this agent yet;
        # such a failure is handled as a crash until it is.
        del screen
        return False

    def startup_answer(self, evidence: str) -> Sequence[str] | None:
        del evidence
        return None

    def permission_summary(self, screen: str) -> str | None:
        del screen  # not offered remotely yet (see permission_keys)
        return None

    def permission_command(self, screen: str) -> str | None:
        # Not read yet: OpenCode's permission dialog goes to a person.
        del screen
        return None

    def permission_keys(self, evidence: str, *, approve: bool) -> Sequence[str] | None:
        # The permission dialog's keys have not been verified yet: answer at the terminal.
        del evidence, approve
        return None
