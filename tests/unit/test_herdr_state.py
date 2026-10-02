"""derive_state against synthetic inputs and against every recorded capture."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from coban.core.model import AgentState
from coban.herdr.state import derive_state

FIXTURES = Path(__file__).parents[2] / "fixtures"
FALLBACK = {
    "state": "idle",
    "matched_rule": None,
    "fallback_reason": "default_known_agent_idle_fallback",
}


def rule(state: str, rule_id: str) -> dict[str, Any]:
    return {"state": state, "matched_rule": {"id": rule_id}, "fallback_reason": None}


def reported(state: str) -> dict[str, Any]:
    """An integration reports lifecycle: no rule, but no fallback either."""
    return {"state": state, "matched_rule": None, "fallback_reason": None}


WORK, BLOCK = AgentState.WORKING, AgentState.BLOCKED_OTHER
IDLE, UNK = AgentState.IDLE, AgentState.UNKNOWN


@pytest.mark.parametrize(
    ("agent_status", "explain", "expected"),
    [
        ("working", rule("working", "osc_title_working"), (WORK, "herdr:rule:osc_title_working")),
        ("blocked", rule("blocked", "bash_permission"), (BLOCK, "herdr:rule:bash_permission")),
        ("idle", rule("idle", "live_prompt_box"), (IDLE, "herdr:rule:live_prompt_box")),
        # A turn ended in a background tab: the screen decides.
        ("done", rule("idle", "live_prompt_box"), (IDLE, "herdr:rule:live_prompt_box")),
        ("done", FALLBACK, (UNK, "herdr:idle-fallback")),
        ("idle", FALLBACK, (UNK, "herdr:idle-fallback")),
        ("working", reported("working"), (WORK, "herdr:reported")),
        ("done", reported("idle"), (IDLE, "herdr:reported")),
        # Seen live with Codex: the pane status lagged behind the working title.
        ("idle", rule("working", "osc_title_working"), (WORK, "herdr:rule:osc_title_working")),
        ("unknown", None, (UNK, "herdr:no-agent")),
        ("unknown", rule("unknown", "transcript_viewer"), (UNK, "herdr:status:unknown")),
    ],
)
def test_derive_state(
    agent_status: str, explain: dict[str, Any] | None, expected: tuple[AgentState, str]
) -> None:
    assert derive_state(agent_status, explain) == expected


def captures(pattern: str) -> list[Path]:
    found = sorted(path.parent for path in FIXTURES.glob(f"{pattern}/*/explain.json"))
    assert found, f"no captures match {pattern}"
    return found


def state_of(capture: Path) -> AgentState:
    pane = json.loads((capture / "pane.json").read_text())
    explain = json.loads((capture / "explain.json").read_text())
    return derive_state(pane.get("agent_status"), None if "error" in explain else explain)[0]


@pytest.mark.parametrize(
    ("pattern", "allowed"),
    [
        # Pickers and first-run dialogs must never look ready for a prompt.
        ("*/resume_prompt", {AgentState.UNKNOWN}),
        ("gemini/*", {AgentState.UNKNOWN}),
        ("*/crashed", {AgentState.UNKNOWN}),
        ("*/working", {AgentState.WORKING}),
        ("claude/idle", {AgentState.IDLE}),
        ("claude/blocked_*", {AgentState.BLOCKED_OTHER}),
        ("opencode/blocked_*", {AgentState.BLOCKED_OTHER}),
        # Codex: start-up dialogs and plain-text questions are invisible to herdr.
        ("codex/blocked_*", {AgentState.BLOCKED_OTHER, AgentState.UNKNOWN}),
        # Codex has no idle rule, so herdr alone can never confirm it is idle.
        ("codex/idle", {AgentState.UNKNOWN}),
        ("codex/done", {AgentState.UNKNOWN}),
    ],
)
def test_recorded_captures_map_to_safe_states(pattern: str, allowed: set[AgentState]) -> None:
    states = {capture: state_of(capture) for capture in captures(pattern)}
    unexpected = {str(c.relative_to(FIXTURES)): s for c, s in states.items() if s not in allowed}
    assert unexpected == {}


def test_no_recorded_dialog_or_picker_is_reported_idle() -> None:
    """The ADR 0004 rule in one assertion: only real prompts are ``idle``."""
    idle = [
        str(capture.relative_to(FIXTURES))
        for capture in captures("*/*")
        if state_of(capture) is AgentState.IDLE
    ]
    assert all(path.split("/")[1] in {"idle", "done"} for path in idle), idle
