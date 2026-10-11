"""Claude Code adapter. Evidence: fixtures/claude/, docs/research/agents.md.

Its screen rules are baton-detect's (crates/baton-detect/rules/claude.toml).
"""

from __future__ import annotations

import json
import re
import shlex
from typing import TYPE_CHECKING

from baton_herdr.core.detection import one_line_summary
from baton_herdr.core.model import AgentKind

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

# herdr manifest rule ids (fixtures/herdr/agent-detection/claude-*.toml) → blocked kind.
# The tool permission prompt opens on "1. Yes"; "Esc to cancel" denies
# (fixtures/claude/blocked_permission/).
PERMISSION_EVIDENCE = frozenset(
    {"herdr:rule:bash_permission_prompt", "herdr:rule:generic_permission_prompt"}
)
APPROVE_KEYS = ("Enter",)
DENY_KEYS = ("Escape",)


def status_line_args(binary: Path, log: Path) -> str:
    """``--settings`` for the Claude sessions baton starts: ``baton-detect statusline`` as
    their status line, logging Claude's rate limits to ``log`` (ADR 0015). Only these
    sessions get it; the user's own settings are not touched."""
    command = f"{shlex.quote(str(binary))} statusline --log {shlex.quote(str(log))}"
    settings = {"statusLine": {"type": "command", "command": command}}
    return f"--settings {shlex.quote(json.dumps(settings, separators=(',', ':')))}"


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

    def trust_answer(self, evidence: str) -> Sequence[str] | None:
        # "No, exit" is selected; "Yes, I trust this folder" is below it (seen live,
        # 2.1.291, fixtures/claude/blocked_permission/20261011T113734Z).
        return ("Down", "Enter") if evidence == "baton:claude_trust_folder" else None

    def permission_summary(self, screen: str) -> str | None:
        # The prompt sits between a full-width rule and "Do you want to proceed?";
        # the command itself is framed by dashed lines (fixtures/claude/blocked_permission/).
        block = _prompt_block(screen)
        if block is None:
            return _tool_summary(screen)
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

    def permission_keys(
        self, evidence: str, *, approve: bool, screen: str = ""
    ) -> Sequence[str] | None:
        if evidence in PERMISSION_EVIDENCE:
            return APPROVE_KEYS if approve else DENY_KEYS
        if evidence == TOOL_PERMISSION_EVIDENCE:
            if not approve:
                return DENY_KEYS
            choice = one_time_yes(screen)
            return (choice,) if choice else None
        return None


TOOL_PERMISSION_EVIDENCE = "baton:claude_tool_permission"
# A numbered choice of a dialog, the selected one marked with U+276F: "4. Yes, but ask
# again next time".
_CHOICE = re.compile(r"^\s*(?:\u276f\s*)?(\d)\.\s+(.+?)\s*$")
# A choice that says yes for this once. Anything that also keeps, always or from now on
# allows is not one: it changes the user's settings beyond this prompt.
_ONE_TIME_YES = re.compile(r"Yes(?:, but ask again next time)?\.?")


def one_time_yes(screen: str) -> str | None:
    """The number of the last dialog's choice that approves this once, or ``None``."""
    lines = screen.splitlines()
    asks = [i for i, line in enumerate(lines) if line.strip().endswith("?")]
    if not asks:
        return None
    for line in lines[asks[-1] + 1 :]:
        if (choice := _CHOICE.match(line)) and _ONE_TIME_YES.fullmatch(choice[2]):
            return choice[1]
    return None


def _tool_summary(screen: str) -> str | None:
    """A tool dialog other than Bash's: its title and the call, under the last full-width
    rule (fixtures/claude/blocked_permission/20261011T114435Z)."""
    lines = screen.splitlines()
    rules = [i for i, line in enumerate(lines) if line.strip().startswith("────")]
    asks = [line for line in lines if line.strip().startswith("Allow this")]
    if not rules or not asks or not asks[-1].strip().endswith("?"):
        return None
    kept = [
        line.strip()
        for line in lines[rules[-1] + 1 :]
        if line.strip() and not line.strip().startswith("╌")
    ][:2]
    return one_line_summary(kept) if kept else None


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
