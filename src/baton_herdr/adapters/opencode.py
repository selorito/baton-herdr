"""OpenCode adapter. Evidence: fixtures/opencode/, docs/research/agents.md.

OpenCode is a full-screen interface: dialogs open in the middle of the screen,
so its rules look at the whole recent screen rather than the bottom lines. With
herdr's OpenCode plugin installed, herdr's working / blocked / idle come from the
plugin; these rules refine them and fill the gaps.
"""

from __future__ import annotations

import re
import shlex
from typing import TYPE_CHECKING

from baton_herdr.core.clock_text import parse_duration
from baton_herdr.core.detection import ScreenRule, detect
from baton_herdr.core.model import AgentKind, AgentState

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from baton_herdr.core.detection import DetectionRequest, DetectionResult

_WHOLE_SCREEN = 200

_PERMISSION = re.compile(r"△ Permission required[\s\S]*?Allow once\s+Allow always\s+Reject")
_QUESTION = re.compile(r"↑↓ select\s+enter submit\s+esc dismiss")
_SESSIONS = re.compile(r"^\W*Sessions\s*(?:esc)?\s*\n(?:.*\n){0,3}?\W*Search\b", re.MULTILINE)
# OpenCode's own retry layer (anomalyco/opencode v1.18.32, session/retry.ts): its
# free-tier and Go plan limits. Provider errors such as "Too Many Requests" are
# retried by OpenCode itself and are not treated as a usage limit here. How these
# lines render on screen has not been captured yet.
_LIMIT = re.compile(
    r"Free limit reached"
    r"|usage limit reached\.\s+It will reset in\s+(?P<in>(?:\d+\s*[a-z]+\s*)+)",
    re.IGNORECASE,
)
# Mode line of the composer ("Build · <model> <provider>") with no progress bar
# ("■⬝⬝… esc interrupt") anywhere near the bottom.
_IDLE = re.compile(r"^\W*(?:Build|Plan) · [^\n]+$(?![\s\S]*esc interrupt)", re.MULTILINE)


def _limit_resets(match: re.Match[str], after: datetime, _zone: ZoneInfo) -> datetime | None:
    if match.group("in"):
        duration = parse_duration(match.group("in"))
        return after + duration if duration is not None else None
    return None


RULES = (
    ScreenRule(
        "opencode_usage_limit",
        AgentState.RATE_LIMITED,
        _LIMIT,
        bottom_lines=_WHOLE_SCREEN,
        reset_parser=_limit_resets,
    ),
    ScreenRule(
        "opencode_permission",
        AgentState.BLOCKED_PERMISSION,
        _PERMISSION,
        bottom_lines=_WHOLE_SCREEN,
    ),
    ScreenRule(
        "opencode_question", AgentState.BLOCKED_QUESTION, _QUESTION, bottom_lines=_WHOLE_SCREEN
    ),
    ScreenRule(
        "opencode_sessions_dialog", AgentState.BLOCKED_OTHER, _SESSIONS, bottom_lines=_WHOLE_SCREEN
    ),
    ScreenRule(
        "opencode_idle_composer",
        AgentState.IDLE,
        _IDLE,
        bottom_lines=12,
        applies_when=frozenset({AgentState.UNKNOWN}),
    ),
)


class OpenCodeAdapter:
    kind = AgentKind.OPENCODE

    def launch_command(self) -> str:
        return "opencode"

    def resume_command(self, session_ref: str) -> str:
        return f"opencode --session {shlex.quote(session_ref)}"

    def classify(self, request: DetectionRequest) -> DetectionResult:
        return detect(request, RULES)

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

    def permission_keys(self, evidence: str, *, approve: bool) -> Sequence[str] | None:
        # The permission dialog's keys have not been verified yet: answer at the terminal.
        del evidence, approve
        return None
