"""Claude Code adapter. Evidence: fixtures/claude/, docs/research/agents.md."""

from __future__ import annotations

import contextlib
import re
import shlex
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from baton_herdr.core.clock_text import next_clock_time
from baton_herdr.core.detection import ScreenRule, detect, one_line_summary, refine_blocked
from baton_herdr.core.model import AgentKind, AgentState

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from baton_herdr.core.detection import DetectionRequest, DetectionResult

# Usage limit lines, per code.claude.com/docs/en/errors: the session, weekly, Opus and
# Sonnet limits read "You've hit your <name> limit", followed by a reset clause such as
# "resets 3:45pm" or "resets Mon 12:00am"; spend limits have no reset clause. A zone name
# in parentheses after the time is honoured if a build prints one. The warning that
# starts with "You've used" is deliberately not matched.
_LIMIT = re.compile(
    r"You've\s+hit\s+your\s+[^\n·]{0,60}?(?:limit|budget)"
    r"(?:\s*·\s*(?:your\s+\w+\s+limit\s+)?resets\s+"
    r"(?:(?P<weekday>Mon|Tue|Wed|Thu|Fri|Sat|Sun)\w*\s+)?"
    r"(?P<clock>\d{1,2}(?::\d{2})?\s*[ap]\.?m\.?)"
    r"(?:\s*\((?P<zone>[A-Za-z_]+/[A-Za-z_/+-]+)\))?)?",
    re.IGNORECASE,
)
_CONTEXT_FULL = re.compile(r"^\s*Context limit reached · /(?:compact|clear)", re.MULTILINE)
_RESUME_PICKER = re.compile(r"Ctrl\+A to show all projects[\s\S]{0,200}?Space to preview")


def _limit_resets(match: re.Match[str], after: datetime, zone: ZoneInfo) -> datetime | None:
    if not match.group("clock"):
        return None
    if match.group("zone"):
        with contextlib.suppress(ZoneInfoNotFoundError, ValueError):
            zone = ZoneInfo(match.group("zone"))
    return next_clock_time(
        match.group("clock"), after=after, zone=zone, weekday=match.group("weekday")
    )


RULES = (
    ScreenRule("claude_usage_limit", AgentState.RATE_LIMITED, _LIMIT, reset_parser=_limit_resets),
    ScreenRule("claude_context_limit", AgentState.CONTEXT_FULL, _CONTEXT_FULL),
    ScreenRule("claude_resume_picker", AgentState.BLOCKED_OTHER, _RESUME_PICKER),
)

# herdr manifest rule ids (fixtures/herdr/agent-detection/claude-*.toml) → blocked kind.
# The tool permission prompt opens on "1. Yes"; "Esc to cancel" denies
# (fixtures/claude/blocked_permission/).
PERMISSION_EVIDENCE = frozenset(
    {"herdr:rule:bash_permission_prompt", "herdr:rule:generic_permission_prompt"}
)
APPROVE_KEYS = ("Enter",)
DENY_KEYS = ("Escape",)

BLOCKED_KINDS = {
    "bash_permission_prompt": AgentState.BLOCKED_PERMISSION,
    "generic_permission_prompt": AgentState.BLOCKED_PERMISSION,
    "live_blocked_form": AgentState.BLOCKED_QUESTION,
    "mcp_elicitation_prompt": AgentState.BLOCKED_QUESTION,
}


class ClaudeAdapter:
    kind = AgentKind.CLAUDE

    def launch_command(self) -> str:
        return "claude"

    def resume_command(self, session_ref: str) -> str:
        return f"claude --resume {shlex.quote(session_ref)}"

    def classify(self, request: DetectionRequest) -> DetectionResult:
        return refine_blocked(detect(request, RULES), BLOCKED_KINDS)

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
