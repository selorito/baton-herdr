"""Screens against every recorded capture and documented limit messages, and the adapters.

Screens are classified by baton-detect (its rules are crates/baton-detect/rules/), as in
batond; these tests are skipped without the binary unless CI requires it. Limit screens
have not been captured yet (they cannot be produced on demand), so
those tests use the wording documented by the vendors and reported in issues;
the sources are in docs/research/agents.md. Replace them with captures when real
limit screens are recorded.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from baton_herdr.adapters import ADAPTERS
from baton_herdr.adapters.claude import ClaudeAdapter
from baton_herdr.adapters.codex import CodexAdapter
from baton_herdr.adapters.opencode import OpenCodeAdapter
from baton_herdr.core.detection import DetectionRequest, DetectionResult
from baton_herdr.core.model import AgentKind, AgentState
from baton_herdr.herdr.state import derive_state

from detector_binary import classify_one

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
        agent_running="error" not in explain,
        observed_at=NOW,
    )
    return classify_one(request)


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
    "codex/blocked_permission/20261006T155659Z": PERM,  # command approval (0.160)
    "codex/blocked_permission/20261006T155726Z": PERM,  # compound command
    "codex/blocked_permission/20261006T155802Z": PERM,  # long command, wrapped
    "codex/blocked_question/20260930T075645Z": OTHER,  # update prompt
    # A plain-text question looks exactly like an idle prompt; the end-of-turn contract
    # in agents.md is what will tell them apart.
    "codex/blocked_question/20260930T075801Z": IDLE,
    "codex/crashed/20260930T075833Z": UNK,
    "codex/resume_prompt/20260930T075906Z": OTHER,
    "opencode/idle/20260930T080210Z": IDLE,  # fresh start; herdr idle is a fallback
    "opencode/idle/20260930T080359Z": IDLE,  # after --continue
    "opencode/done/20260930T080234Z": IDLE,  # idle reported by herdr's OpenCode plugin
    "opencode/working/20260930T080214Z": WORK,
    "opencode/blocked_permission/20260930T080259Z": PERM,
    "opencode/blocked_question/20260930T080318Z": QUESTION,
    # The dead TUI's last frame stays under the shell prompt; only herdr knows it exited.
    "opencode/crashed/20260930T080341Z": UNK,
    "opencode/resume_prompt/20260930T080420Z": OTHER,  # /sessions dialog
}


def test_every_v1_agent_capture_is_listed() -> None:
    captures = {
        str(p.parent.relative_to(FIXTURES))
        for agent in ("claude", "codex", "opencode")
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
    return classify_one(request)


def codex(text: str, zone: str = "Europe/Istanbul") -> DetectionResult:
    request = DetectionRequest(agent=AgentKind.CODEX, screen=text, observed_at=NOW, timezone=zone)
    return classify_one(request)


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
        "baton:claude_usage_limit",
        resets_at,
    )


def test_claude_limit_warning_is_not_a_limit() -> None:
    warning = screen("You've used 85% of your session limit · resets 3:45pm", "❯")
    assert claude(warning, host=AgentState.IDLE).state is AgentState.IDLE


def test_claude_context_limit() -> None:
    result = claude(screen("Context limit reached · /compact or /clear to continue"))
    assert (result.state, result.evidence) == (
        AgentState.CONTEXT_FULL,
        "baton:claude_context_limit",
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
    result = codex(screen(text, "› Ask Codex to do anything", "  gpt · ~/dev/baton-sandbox"))
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
    assert OpenCodeAdapter().resume_command("ses_f0ea") == "opencode --session ses_f0ea"
    assert {kind: a.launch_command() for kind, a in ADAPTERS.items()} == {
        AgentKind.CLAUDE: "claude",
        AgentKind.CODEX: "codex",
        AgentKind.OPENCODE: "opencode",
    }


def test_codex_work_is_recognised_from_the_footer_spinner() -> None:
    working = screen(
        "• Working (2s • esc to interrupt)", "› Ask Codex to do anything", "  gpt · ~/dev · ⠏"
    )
    result = codex(working)
    assert (result.state, result.evidence) == (AgentState.WORKING, "baton:codex_working_footer")
    idle = screen("› Ask Codex to do anything", "  gpt · ~/dev/baton-sandbox")
    assert codex(idle).state is AgentState.IDLE


def opencode(text: str) -> DetectionResult:
    request = DetectionRequest(agent=AgentKind.OPENCODE, screen=text, observed_at=NOW)
    return classify_one(request)


@pytest.mark.parametrize(
    ("line", "resets_at"),
    [
        ("Free limit reached", None),
        (
            "Go usage limit reached. It will reset in 2 hours 15 minutes. To continue using "
            "this model now, enable usage from your available balance",
            datetime(2026, 10, 1, 13, 15, tzinfo=UTC),
        ),
    ],
)
def test_opencode_usage_limits(line: str, resets_at: datetime | None) -> None:
    result = opencode(screen(f"  ┃  {line}", "  ┃  Build · Big Pickle OpenCode Zen"))
    assert (result.state, result.resets_at) == (AgentState.RATE_LIMITED, resets_at)


def test_opencode_provider_throttling_is_left_to_opencodes_own_retry() -> None:
    result = opencode(screen("  ┃  Too Many Requests", "  ┃  Build · Big Pickle OpenCode Zen"))
    assert result.state is AgentState.IDLE


def test_no_rule_applies_when_the_host_sees_no_agent() -> None:
    limit = screen("You've hit your session limit · resets 3:45pm", "❯")
    request = DetectionRequest(
        agent=AgentKind.CLAUDE,
        screen=limit,
        host_evidence="herdr:no-agent",
        agent_running=False,
        observed_at=NOW,
    )
    result = classify_one(request)
    assert (result.state, result.evidence) == (AgentState.UNKNOWN, "herdr:no-agent")


def test_only_the_codex_update_prompt_is_answered_automatically() -> None:
    answers = {
        (kind, rule): adapter.startup_answer(f"baton:{rule}")
        for kind, adapter in ADAPTERS.items()
        for rule in (
            "codex_update_prompt",
            "codex_trust_folder",
            "codex_hooks_review",
            "claude_resume_picker",
            "opencode_sessions_dialog",
        )
    }
    assert {key: keys for key, keys in answers.items() if keys} == {
        (AgentKind.CODEX, "codex_update_prompt"): ("Down", "Enter")
    }


def _screen(capture: str) -> str:
    return (FIXTURES / capture / "screen.detection.txt").read_text()


def test_permission_prompts_are_summarised_for_the_operator() -> None:
    claude = ClaudeAdapter().permission_summary(
        _screen("claude/blocked_permission/20260930T072344Z")
    )
    assert (
        claude == "Bash command · python3 -m unittest -v 2>&1 · Run the unit test suite verbosely"
    )
    codex = CodexAdapter().permission_summary(_screen("codex/blocked_permission/20260930T075730Z"))
    assert codex is not None
    assert codex.startswith("Would you like to make the following edits? · Description:")
    assert "1. Yes" not in codex
    assert (
        ClaudeAdapter().permission_summary(_screen("claude/idle/" + _first("claude/idle"))) is None
    )


def _first(kind: str) -> str:
    return sorted(p.name for p in (FIXTURES / kind).iterdir() if p.is_dir())[0]


@pytest.mark.parametrize(
    ("agent", "evidence", "approve", "deny"),
    [
        (AgentKind.CLAUDE, "herdr:rule:bash_permission_prompt", ("Enter",), ("Escape",)),
        (AgentKind.CODEX, "baton:codex_edit_or_command_approval", ("y",), ("Escape",)),
        # Never remotely: folder trust, hook review, unknown prompts, unverified agents.
        (AgentKind.CODEX, "baton:codex_trust_folder", None, None),
        (AgentKind.CODEX, "baton:codex_hooks_review", None, None),
        (AgentKind.CLAUDE, "herdr:rule:live_blocked_form", None, None),
        (AgentKind.OPENCODE, "herdr:rule:permission_prompt", None, None),
    ],
)
def test_permission_keys_only_for_verified_tool_prompts(
    agent: AgentKind,
    evidence: str,
    approve: tuple[str, ...] | None,
    deny: tuple[str, ...] | None,
) -> None:
    adapter = ADAPTERS[agent]
    assert adapter.permission_keys(evidence, approve=True) == approve
    assert adapter.permission_keys(evidence, approve=False) == deny


def _bash_prompt(*body: str) -> str:
    lines = "\n".join(f"   {line}" for line in body)
    return f"─────────\n Bash command\n Tip: x\n\n{lines}\n\n Do you want to proceed?\n ❯ 1. Yes\n"


def test_the_command_of_a_permission_prompt_is_read_for_the_policy() -> None:
    claude = ClaudeAdapter()
    recorded = _screen("claude/blocked_permission/20260930T072344Z")
    assert claude.permission_command(recorded) == "python3 -m unittest -v 2>&1"
    # Only a last line that reads as prose is taken for the description.
    assert claude.permission_command(_bash_prompt("ls", "List the files")) == "ls"
    assert claude.permission_command(_bash_prompt("ls")) == "ls"
    # Not prose: kept as part of the command, where the policy asks about it.
    assert claude.permission_command(_bash_prompt("pytest", "rm -rf /")) == "pytest\nrm -rf /"
    assert (
        claude.permission_command(_bash_prompt("pytest", "Remove /tmp/x"))
        == "pytest\nRemove /tmp/x"
    )
    # Not a shell command, or no prompt at all.
    edit = "─────\n Edit file\n calc.py\n Do you want to proceed?\n"
    assert claude.permission_command(edit) is None
    assert claude.permission_command(_screen("claude/idle/" + _first("claude/idle"))) is None


def test_codex_command_approvals_are_read_only_when_the_command_fits_on_one_line() -> None:
    codex = CodexAdapter()
    assert codex.permission_command(_screen("codex/blocked_permission/20261006T155659Z")) == (
        "touch build.log"
    )
    assert codex.permission_command(_screen("codex/blocked_permission/20261006T155726Z")) == (
        "mkdir -p build && touch build/out.txt && ls build"
    )
    # Wrapped over four unmarked lines: it cannot be rebuilt with certainty.
    assert codex.permission_command(_screen("codex/blocked_permission/20261006T155802Z")) is None
    # An edit approval has no command.
    assert codex.permission_command(_screen("codex/blocked_permission/20260930T075730Z")) is None
    prompt = (
        "  Would you like to run the following command?\n\n  $ ls\n{}\n\n› 1. Yes, proceed (y)\n"
    )
    assert codex.permission_command(prompt.format("  -la")) is None  # a second line
    assert codex.permission_command(prompt.format("")) == "ls"
