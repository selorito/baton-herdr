"""Claude Code adapter. Evidence: fixtures/claude/, docs/research/agents.md."""

from __future__ import annotations

import contextlib
import re
import shlex
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from coban.core.clock_text import next_clock_time
from coban.core.detection import ScreenRule, detect, refine_blocked
from coban.core.model import AgentKind, AgentState

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from coban.core.detection import DetectionRequest, DetectionResult

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

    def startup_answer(self, evidence: str) -> Sequence[str] | None:
        del evidence
        return None
