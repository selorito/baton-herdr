"""Codex CLI adapter. Evidence: fixtures/codex/, docs/research/agents.md.

Its screen rules are baton-detect's (crates/baton-detect/rules/codex.toml).
"""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

from baton_herdr.core.detection import one_line_summary
from baton_herdr.core.model import AgentKind

if TYPE_CHECKING:
    from collections.abc import Sequence

# Start-up dialogs baton may answer on its own. Only the update prompt qualifies: its
# answer ("2. Skip", one row down from the default) keeps the tested version and has no
# security meaning. Folder trust and hook review stay with a person.
# "1. Yes, proceed (y)" and "esc to cancel" (fixtures/codex/blocked_permission/).
APPROVE_KEYS = ("y",)
COMMAND_APPROVAL = "Would you like to run the following command?"
DENY_KEYS = ("Escape",)
STARTUP_ANSWERS: dict[str, tuple[str, ...]] = {"baton:codex_update_prompt": ("Down", "Enter")}


class CodexAdapter:
    kind = AgentKind.CODEX

    def launch_command(self) -> str:
        return "codex"

    def resume_command(self, session_ref: str) -> str:
        return f"codex resume {shlex.quote(session_ref)}"

    def resume_failed(self, screen: str) -> bool:
        # The wording of a failed resume has not been observed for this agent yet;
        # such a failure is handled as a crash until it is.
        del screen
        return False

    def startup_answer(self, evidence: str) -> Sequence[str] | None:
        return STARTUP_ANSWERS.get(evidence)

    def permission_summary(self, screen: str) -> str | None:
        # "Would you like to ...?" and its details, down to the first option
        # (fixtures/codex/blocked_permission/).
        lines = screen.splitlines()
        anchors = [i for i, line in enumerate(lines) if "Would you like to" in line]
        if not anchors:
            return None
        kept: list[str] = []
        for line in lines[anchors[-1] :]:
            text = line.strip()
            if text.startswith(("\u203a", "1.")):  # the option cursor
                break
            kept.append(text)
        return one_line_summary(kept)

    def permission_command(self, screen: str) -> str | None:
        # A command approval shows the command on a line of its own after "$ ", then a
        # blank line (fixtures/codex/blocked_permission/20261006T*). A long command wraps
        # onto unmarked lines, sometimes inside a word, so it cannot be put back together
        # with certainty: then, as for edits, a person decides.
        lines = screen.splitlines()
        anchors = [i for i, line in enumerate(lines) if COMMAND_APPROVAL in line]
        if not anchors:
            return None
        rest = lines[anchors[-1] + 1 :]
        starts = [i for i, line in enumerate(rest) if line.strip().startswith("$ ")]
        if len(starts) != 1:
            return None
        start = starts[0]
        if start + 1 >= len(rest) or rest[start + 1].strip():
            return None  # wrapped, or more than one line
        return rest[start].strip().removeprefix("$ ").strip() or None

    def permission_keys(self, evidence: str, *, approve: bool) -> Sequence[str] | None:
        if evidence != "baton:codex_edit_or_command_approval":
            return None  # folder trust and hook review stay at the terminal
        return APPROVE_KEYS if approve else DENY_KEYS
