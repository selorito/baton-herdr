"""Adapters against every recorded capture, and against documented limit messages.

Limit screens have not been captured yet (they cannot be produced on demand), so
those tests use the wording documented by the vendors and reported in issues;
the sources are in docs/research/agents.md. Replace them with captures when real
limit screens are recorded.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from coban.adapters import ADAPTERS
from coban.adapters.claude import ClaudeAdapter
from coban.adapters.codex import CodexAdapter
from coban.core.detection import DetectionRequest, DetectionResult
from coban.core.model import AgentKind, AgentState
from coban.herdr.state import derive_state

FIXTURES = Path(__file__).parents[2] / "fixtures"
NOW = datetime(2026, 10, 1, 11, 0, tzinfo=UTC)  # Thursday, 14:00 in Istanbul
AT_1545 = datetime(2026, 10, 1, 12, 45, tzinfo=UTC)  # 15:45 Istanbul, later today


def classify_capture(capture: str) -> DetectionResult:
    directory = FIXTURES / capture
    pane = json.loads((directory / "pane.json").read_text())
    explain = json.loads((directory / "explain.json").read_text())
    host_state, host_evidence = derive_state(
        pane.get("agent_status"), None if "error" in explain else explain
    )
    agent = AgentKind(capture.split("/", maxsplit=1)[0])
    request = DetectionRequest(
        agent=agent,
        screen=(directory / "screen.detection.txt").read_text(),
        host_state=host_state,
        host_evidence=host_evidence,
        observed_at=NOW,
    )
    return ADAPTERS[agent].classify(request)


IDLE, WORK = AgentState.IDLE, AgentState.WORKING
PERM, QUESTION, OTHER = (
    AgentState.BLOCKED_PERMISSION,
    AgentState.BLOCKED_QUESTION,
    AgentState.BLOCKED_OTHER,
)
UNK = AgentState.UNKNOWN

EXPECTED = {
    "claude/idle/20260930T071250Z": IDLE,
    "claude/idle/20260930T074402Z": IDLE,
    "claude/idle/20260930T075346Z": IDLE,
    "claude/working/20260930T072147Z": WORK,
    "claude/blocked_permission/20260930T072344Z": PERM,
    "claude/blocked_question/20260930T072744Z": QUESTION,
    "claude/done/20260930T074252Z": IDLE,
    "claude/crashed/20260930T074646Z": UNK,
    "claude/resume_prompt/20260930T075339Z": OTHER,
    "codex/idle/20260930T075545Z": IDLE,
    "codex/idle/20260930T075913Z": IDLE,
    "codex/done/20260930T075601Z": IDLE,  # turn ended with an API error line above the prompt
    "codex/done/20260930T075714Z": IDLE,
    "codex/working/20260930T075548Z": WORK,
    "codex/working/20260930T075656Z": WORK,
    "codex/blocked_permission/20260930T075506Z": OTHER,  # folder trust
    "codex/blocked_permission/20260930T075527Z": OTHER,  # hooks review
    "codex/blocked_permission/20260930T075730Z": PERM,  # edit approval
    "codex/blocked_question/20260930T075645Z": OTHER,  # update prompt
    # A plain-text question looks exactly like an idle prompt; the end-of-turn contract
    # in agents.md is what will tell them apart.
    "codex/blocked_question/20260930T075801Z": IDLE,
    "codex/crashed/20260930T075833Z": UNK,
    "codex/resume_prompt/20260930T075906Z": OTHER,
}


def test_every_claude_and_codex_capture_is_listed() -> None:
    captures = {
        str(p.parent.relative_to(FIXTURES))
        for agent in ("claude", "codex")
        for p in (FIXTURES / agent).glob("*/*/explain.json")
    }
    assert captures == set(EXPECTED)


@pytest.mark.parametrize(("capture", "expected"), sorted(EXPECTED.items()))
def test_recorded_screens(capture: str, expected: AgentState) -> None:
    assert classify_capture(capture).state is expected


def screen(*lines: str) -> str:
    return "\n".join(["previous output", *lines, ""])


def claude(
    text: str, host: AgentState = AgentState.UNKNOWN, zone: str = "Europe/Istanbul"
) -> DetectionResult:
    request = DetectionRequest(
        agent=AgentKind.CLAUDE, screen=text, host_state=host, observed_at=NOW, timezone=zone
    )
    return ClaudeAdapter().classify(request)


def codex(text: str, zone: str = "Europe/Istanbul") -> DetectionResult:
    request = DetectionRequest(agent=AgentKind.CODEX, screen=text, observed_at=NOW, timezone=zone)
    return CodexAdapter().classify(request)


@pytest.mark.parametrize(
    ("line", "resets_at"),
    [
        (
            "You've hit your session limit · resets 3:45pm",
            datetime(2026, 10, 1, 12, 45, tzinfo=UTC),
        ),
        ("You've hit your session limit · resets 1pm", datetime(2026, 10, 2, 10, 0, tzinfo=UTC)),
        (
            "You've hit your weekly limit · resets Mon 12:00am",
            datetime(2026, 10, 4, 21, 0, tzinfo=UTC),
        ),
        ("You've hit your Opus limit · resets 3:45pm", AT_1545),
        (
            "You've hit your session limit · resets 3:45pm (America/New_York)",
            datetime(2026, 10, 1, 19, 45, tzinfo=UTC),
        ),
        ("You've hit your monthly spend limit", None),
    ],
)
def test_claude_usage_limits(line: str, resets_at: datetime | None) -> None:
    # herdr reports such a screen as idle; the limit must still win.
    result = claude(screen(line, "❯"), host=AgentState.IDLE)
    assert (result.state, result.evidence, result.resets_at) == (
        AgentState.RATE_LIMITED,
        "coban:claude_usage_limit",
        resets_at,
    )


def test_claude_limit_warning_is_not_a_limit() -> None:
    warning = screen("You've used 85% of your session limit · resets 3:45pm", "❯")
    assert claude(warning, host=AgentState.IDLE).state is AgentState.IDLE


def test_claude_context_limit() -> None:
    result = claude(screen("Context limit reached · /compact or /clear to continue"))
    assert (result.state, result.evidence) == (
        AgentState.CONTEXT_FULL,
        "coban:claude_context_limit",
    )


@pytest.mark.parametrize(
    ("text", "resets_at"),
    [
        (
            "■ You've hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro), "
            "visit https://chatgpt.com/codex/settings/usage to purchase more credits or "
            "try again at "
            "Feb 23rd, 2026 9:01 PM.",
            datetime(2026, 2, 23, 18, 1, tzinfo=UTC),
        ),
        (
            "■ You've hit your usage limit. Upgrade to Pro (https://openai.com/chatgpt/pricing) or "
            "try again in 3 hours 2 minutes.",
            datetime(2026, 10, 1, 14, 2, tzinfo=UTC),
        ),
        ("■ You've hit your usage limit. Upgrade to Plus to continue using Codex", None),
    ],
)
def test_codex_usage_limits(text: str, resets_at: datetime | None) -> None:
    result = codex(screen(text, "› Ask Codex to do anything", "  gpt · ~/dev/coban-sandbox"))
    assert (result.state, result.resets_at) == (AgentState.RATE_LIMITED, resets_at)


def test_codex_limit_text_wrapped_across_lines_is_still_read() -> None:
    wrapped = screen(
        "■ You've hit your usage limit. To get more access now, send a request to your",
        "admin or try again at Apr 12th, 2026",
        "3:31 PM.",
    )
    assert codex(wrapped, zone="UTC").resets_at == datetime(2026, 4, 12, 15, 31, tzinfo=UTC)


def test_resume_commands_quote_the_session_reference() -> None:
    assert ClaudeAdapter().resume_command("277e10ad-5928") == "claude --resume 277e10ad-5928"
    assert CodexAdapter().resume_command("a b") == "codex resume 'a b'"
    assert {kind: a.launch_command() for kind, a in ADAPTERS.items()} == {
        AgentKind.CLAUDE: "claude",
        AgentKind.CODEX: "codex",
    }


def test_codex_work_is_recognised_from_the_footer_spinner() -> None:
    working = screen(
        "• Working (2s • esc to interrupt)", "› Ask Codex to do anything", "  gpt · ~/dev · ⠏"
    )
    result = codex(working)
    assert (result.state, result.evidence) == (AgentState.WORKING, "coban:codex_working_footer")
    idle = screen("› Ask Codex to do anything", "  gpt · ~/dev/coban-sandbox")
    assert codex(idle).state is AgentState.IDLE
