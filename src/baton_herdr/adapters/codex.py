"""Codex CLI adapter. Evidence: fixtures/codex/, docs/research/agents.md."""

from __future__ import annotations

import re
import shlex
from typing import TYPE_CHECKING

from baton_herdr.core.clock_text import parse_duration, parse_month_date_time
from baton_herdr.core.detection import ScreenRule, detect, one_line_summary, refine_blocked
from baton_herdr.core.model import AgentKind, AgentState

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from baton_herdr.core.detection import DetectionRequest, DetectionResult

# "You've hit your usage limit. … try again at Feb 23rd, 2026 9:01 PM." or
# "… try again in 3 hours 2 minutes." (openai/codex issues #3031, #12299, #16917).
# The screen may wrap the sentence, so whitespace includes newlines.
_LIMIT = re.compile(
    r"You've\s+hit\s+your\s+usage\s+limit\.[\s\S]{0,300}?try\s+again\s+"
    r"(?:at\s+(?P<at>[A-Z][a-z]{2}\w*\.?\s+\d{1,2}\w*,?\s+\d{4},?\s+\d{1,2}:\d{2}\s*[AP]M)"
    r"|in\s+(?P<in>(?:\d+\s+\w+\s*)+))"
    r"|You've\s+hit\s+your\s+usage\s+limit\.",
)
# Codex marks the composer and the selected dialog option with U+203A; numbered lines
# after it are dialog options, not the composer. While a turn runs the footer ends with a
# braille spinner (U+2800..U+28FF).
_MARK = "\u203a"
_SPINNER = "\u2800-\u28ff"
_IDLE_PROMPT = re.compile(rf"^{_MARK} (?!\d+\.\s)[^\n]*\n {{2}}\S[^\n]* · [^\n]*[^\n{_SPINNER}]\Z")
# The footer while a turn runs: same shape as above, ending in a spinner frame.
_WORKING_FOOTER = re.compile(rf"^ {{2}}\S[^\n]* · [^\n]*[{_SPINNER}]\Z")
_TRUST = re.compile(rf"Trust this folder\?[\s\S]*?^{_MARK} 1\. Trust and continue", re.MULTILINE)
_HOOKS = re.compile(rf"Hooks need review[\s\S]*?^{_MARK} 1\. Review hooks", re.MULTILINE)
_UPDATE = re.compile(r"Update available[\s\S]*?Skip until next version")
_RESUME_PICKER = re.compile(r"Resume a previous session[\s\S]*?enter resume")
_APPROVAL = re.compile(
    r"(?:Would you like to make the following edits\?|Allow command\?)"
    r"[\s\S]*?Press enter to confirm or esc to cancel"
)


def _limit_resets(match: re.Match[str], after: datetime, zone: ZoneInfo) -> datetime | None:
    if match.group("at"):
        return parse_month_date_time(" ".join(match.group("at").split()), zone=zone)
    if match.group("in"):
        duration = parse_duration(match.group("in"))
        return after + duration if duration is not None else None
    return None


_ONLY_WITHOUT_HOST_SIGNAL = frozenset({AgentState.UNKNOWN})

RULES = (
    ScreenRule("codex_usage_limit", AgentState.RATE_LIMITED, _LIMIT, reset_parser=_limit_resets),
    ScreenRule("codex_edit_or_command_approval", AgentState.BLOCKED_PERMISSION, _APPROVAL),
    ScreenRule("codex_trust_folder", AgentState.BLOCKED_OTHER, _TRUST),
    ScreenRule("codex_hooks_review", AgentState.BLOCKED_OTHER, _HOOKS),
    ScreenRule("codex_update_prompt", AgentState.BLOCKED_OTHER, _UPDATE),
    ScreenRule("codex_resume_picker", AgentState.BLOCKED_OTHER, _RESUME_PICKER),
    # herdr recognises work from the spinner in the window title; this also catches it
    # from the footer when the title is not available.
    ScreenRule(
        "codex_working_footer",
        AgentState.WORKING,
        _WORKING_FOOTER,
        bottom_lines=1,
        applies_when=_ONLY_WITHOUT_HOST_SIGNAL,
    ),
    # herdr has no idle rule for Codex, so its idle is always a fallback (unknown).
    ScreenRule(
        "codex_idle_prompt",
        AgentState.IDLE,
        _IDLE_PROMPT,
        bottom_lines=2,
        applies_when=_ONLY_WITHOUT_HOST_SIGNAL,
    ),
)


# Start-up dialogs baton may answer on its own. Only the update prompt qualifies: its
# answer ("2. Skip", one row down from the default) keeps the tested version and has no
# security meaning. Folder trust and hook review stay with a person.
# "1. Yes, proceed (y)" and "esc to cancel" (fixtures/codex/blocked_permission/).
APPROVE_KEYS = ("y",)
DENY_KEYS = ("Escape",)
STARTUP_ANSWERS: dict[str, tuple[str, ...]] = {"baton:codex_update_prompt": ("Down", "Enter")}


class CodexAdapter:
    kind = AgentKind.CODEX

    def launch_command(self) -> str:
        return "codex"

    def resume_command(self, session_ref: str) -> str:
        return f"codex resume {shlex.quote(session_ref)}"

    def classify(self, request: DetectionRequest) -> DetectionResult:
        return refine_blocked(detect(request, RULES), {})

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

    def permission_keys(self, evidence: str, *, approve: bool) -> Sequence[str] | None:
        if evidence != "baton:codex_edit_or_command_approval":
            return None  # folder trust and hook review stay at the terminal
        return APPROVE_KEYS if approve else DENY_KEYS
