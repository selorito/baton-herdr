"""Claude Code adapter. Evidence: fixtures/claude/, docs/research/agents.md.

Its screen rules are baton-detect's (crates/baton-detect/rules/claude.toml).
"""

from __future__ import annotations

import re
import shlex
from typing import TYPE_CHECKING

from baton_herdr.core.detection import one_line_summary
from baton_herdr.core.model import AgentKind

if TYPE_CHECKING:
    from collections.abc import Sequence

# herdr manifest rule ids (fixtures/herdr/agent-detection/claude-*.toml) → blocked kind.
# The tool permission prompt opens on "1. Yes"; "Esc to cancel" denies
# (fixtures/claude/blocked_permission/).
PERMISSION_EVIDENCE = frozenset(
    {"herdr:rule:bash_permission_prompt", "herdr:rule:generic_permission_prompt"}
)
APPROVE_KEYS = ("Enter",)
DENY_KEYS = ("Escape",)


class ClaudeAdapter:
    kind = AgentKind.CLAUDE

    def launch_command(self) -> str:
        return "claude"

    def resume_command(self, session_ref: str) -> str:
        return f"claude --resume {shlex.quote(session_ref)}"

    def resume_failed(self, screen: str) -> bool:
        # Printed by `claude --resume <id>` before it exits (seen live, 2.1.285; also in
        # code.claude.com/docs/en/sessions).
        return "No conversation found with session ID" in screen

    def startup_answer(self, evidence: str) -> Sequence[str] | None:
        del evidence
        return None

    def permission_summary(self, screen: str) -> str | None:
        # The prompt sits between a full-width rule and "Do you want to proceed?";
        # the command itself is framed by dashed lines (fixtures/claude/blocked_permission/).
        block = _prompt_block(screen)
        if block is None:
            return None
        kept = [
            text
            for text in block
            if not text.startswith(("╌", "Tip:")) and text != "This command requires approval"
        ]
        return one_line_summary(kept)

    def permission_command(self, screen: str) -> str | None:
        # "Bash command", an optional tip, then the command and its one-line description
        # (fixtures/claude/blocked_permission/). Plain text does not mark where the command
        # ends, so only a last line that reads as prose is taken for the description;
        # anything else stays in the command, where the policy asks about it.
        block = _prompt_block(screen)
        if block is None or "Bash command" not in block:
            return None
        lines: list[str] = []
        for text in block[block.index("Bash command") + 1 :]:
            if text.startswith(("Tip:", "╌")) or (not text and not lines):
                continue
            if not text:
                break
            lines.append(text)
        if len(lines) > 1 and _DESCRIPTION.fullmatch(lines[-1]):
            lines.pop()
        return "\n".join(lines) or None

    def permission_keys(self, evidence: str, *, approve: bool) -> Sequence[str] | None:
        if evidence not in PERMISSION_EVIDENCE:
            return None
        return APPROVE_KEYS if approve else DENY_KEYS


# A tool description: a capitalized word, more words, nothing a shell would treat specially.
_DESCRIPTION = re.compile(r"[A-Z][a-z]+(?: [^\s/=|;&$<>`\\*~]+)+")


def _prompt_block(screen: str) -> list[str] | None:
    """The stripped lines of the newest permission prompt, between its rule and question."""
    lines = screen.splitlines()
    anchors = [i for i, line in enumerate(lines) if "Do you want to proceed?" in line]
    if not anchors:
        return None
    kept: list[str] = []
    for line in reversed(lines[: anchors[-1]]):
        text = line.strip()
        if text.startswith("─"):
            break
        kept.append(text)
    return list(reversed(kept))
