from __future__ import annotations

import pytest

from coban.core.model import AgentState, InterruptReason
from coban.recovery.policy import Verdict, assess, hands_off, interrupt_reason


@pytest.mark.parametrize(
    ("state", "worked", "verdict"),
    [
        (AgentState.WORKING, False, Verdict.CONTINUE),
        (AgentState.WORKING, True, Verdict.CONTINUE),
        # Idle before any work: the prompt has not been picked up yet.
        (AgentState.IDLE, False, Verdict.CONTINUE),
        (AgentState.IDLE, True, Verdict.TURN_FINISHED),
        (AgentState.UNKNOWN, True, Verdict.CONTINUE),
        (AgentState.RATE_LIMITED, False, Verdict.INTERRUPT),
        (AgentState.RATE_LIMITED, True, Verdict.INTERRUPT),
        (AgentState.CONTEXT_FULL, True, Verdict.INTERRUPT),
        (AgentState.CRASHED, True, Verdict.INTERRUPT),
        (AgentState.BLOCKED_PERMISSION, True, Verdict.NEEDS_HUMAN),
        (AgentState.BLOCKED_QUESTION, True, Verdict.NEEDS_HUMAN),
        (AgentState.BLOCKED_OTHER, False, Verdict.NEEDS_HUMAN),
    ],
)
def test_assess(state: AgentState, worked: bool, verdict: Verdict) -> None:
    assert assess(state, worked=worked) is verdict


def test_every_agent_state_has_a_verdict() -> None:
    for state in AgentState:
        verdict = assess(state, worked=True)
        if verdict is Verdict.INTERRUPT:
            assert isinstance(interrupt_reason(state), InterruptReason)


def test_only_a_usage_limit_hands_the_task_to_another_agent() -> None:
    assert {r for r in InterruptReason if hands_off(r)} == {InterruptReason.RATE_LIMITED}
