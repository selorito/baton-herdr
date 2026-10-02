from __future__ import annotations

import pytest

from coban.core.model import AgentState, InterruptReason
from coban.recovery.policy import Plan, Verdict, assess, interrupt_reason, plan_recovery


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


LIMIT, CRASH, STALL, FULL, OPERATOR = (
    InterruptReason.RATE_LIMITED,
    InterruptReason.CRASHED,
    InterruptReason.STALLED,
    InterruptReason.CONTEXT_FULL,
    InterruptReason.OPERATOR,
)


@pytest.mark.parametrize(
    ("reason", "session", "agent_ok", "other_ok", "used", "plan"),
    [
        # Usage limit: same session once the agent is back, else move on, else wait.
        (LIMIT, True, True, True, 0, Plan.RESUME),
        (LIMIT, True, False, True, 0, Plan.HAND_OFF),
        (LIMIT, False, True, False, 0, Plan.HAND_OFF),  # no session to resume: start afresh
        (LIMIT, True, False, False, 0, Plan.WAIT),
        (LIMIT, True, True, True, 9, Plan.RESUME),  # limits are not counted as failures
        # Crash or stall: a bounded number of resumes, then a person.
        (CRASH, True, True, False, 0, Plan.RESUME),
        (STALL, True, True, False, 1, Plan.RESUME),
        (CRASH, True, True, True, 2, Plan.ASK_HUMAN),
        (CRASH, False, True, True, 0, Plan.ASK_HUMAN),
        (CRASH, True, False, True, 0, Plan.ASK_HUMAN),
        # Always a person.
        (FULL, True, True, True, 0, Plan.ASK_HUMAN),
        (OPERATOR, True, True, True, 0, Plan.ASK_HUMAN),
    ],
)
def test_plan_recovery(  # noqa: PLR0913, PLR0917 - one parameter per table column
    reason: InterruptReason, session: bool, agent_ok: bool, other_ok: bool, used: int, plan: Plan
) -> None:
    assert (
        plan_recovery(
            reason,
            has_session=session,
            agent_available=agent_ok,
            another_agent_available=other_ok,
            failure_resumes_used=used,
            max_failure_resumes=2,
        )
        is plan
    )
