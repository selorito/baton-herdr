from __future__ import annotations

import pytest

from baton_herdr.core.model import AgentState, InterruptReason
from baton_herdr.recovery.policy import (
    Economy,
    Plan,
    Verdict,
    assess,
    interrupt_reason,
    plan_recovery,
)


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
        (CRASH, False, True, True, 0, Plan.HAND_OFF),  # it never got going: nothing lost
        (CRASH, True, False, True, 0, Plan.ASK_HUMAN),
        # The session is gone: start afresh, on any available agent, or wait.
        (InterruptReason.RESUME_FAILED, True, True, False, 0, Plan.RESTART),
        (InterruptReason.RESUME_FAILED, True, False, True, 0, Plan.RESTART),
        (InterruptReason.RESUME_FAILED, True, False, False, 0, Plan.WAIT),
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


@pytest.mark.parametrize(
    ("reason", "agent_ok", "other_ok", "failed_starts", "plan"),
    [
        # An agent that exited as it started, or never came up, had no session: the task
        # goes to another agent, else the same one starts afresh, else it waits.
        (CRASH, True, True, 0, Plan.HAND_OFF),
        (STALL, True, False, 1, Plan.RESTART),
        (CRASH, False, False, 0, Plan.WAIT),
        # As many times as resumes are allowed; then a person.
        (CRASH, True, True, 2, Plan.ASK_HUMAN),
        (STALL, True, False, 2, Plan.ASK_HUMAN),
        # A limit or a full context without a session keeps its own rule.
        (FULL, True, True, 0, Plan.ASK_HUMAN),
    ],
)
def test_a_failed_start_is_tried_again_elsewhere_a_bounded_number_of_times(
    reason: InterruptReason, agent_ok: bool, other_ok: bool, failed_starts: int, plan: Plan
) -> None:
    assert (
        plan_recovery(
            reason,
            has_session=False,
            agent_available=agent_ok,
            another_agent_available=other_ok,
            failure_resumes_used=0,
            max_failure_resumes=2,
            failed_starts_used=failed_starts,
        )
        is plan
    )


def test_a_signed_out_agent_hands_the_task_on_or_asks_for_a_sign_in() -> None:
    def plan(other: bool) -> Plan:
        return plan_recovery(
            InterruptReason.SIGNED_OUT,
            has_session=False,
            agent_available=False,
            another_agent_available=other,
            failure_resumes_used=0,
            max_failure_resumes=2,
        )

    assert plan(other=True) is Plan.HAND_OFF
    assert plan(other=False) is Plan.ASK_HUMAN


def economy_plan(  # noqa: PLR0913 - one fact per argument, as plan_recovery takes them
    reason: InterruptReason,
    *,
    failures: int,
    after: int = 1,
    counts: bool = True,
    has_session: bool = True,
    resumes_used: int = 0,
    available: bool = True,
) -> Plan:
    return plan_recovery(
        reason,
        has_session=has_session,
        agent_available=available,
        another_agent_available=False,
        failure_resumes_used=resumes_used,
        max_failure_resumes=2,
        economy=Economy(counts=counts, failures=failures, after=after),
    )


def test_an_economy_stall_moves_the_task_up_once_enough_failures_add_up() -> None:
    assert economy_plan(InterruptReason.STALLED, failures=1) is Plan.ESCALATE
    # Below the count: the same tier afresh; the stuck session is not resumed.
    assert economy_plan(InterruptReason.STALLED, failures=1, after=2) is Plan.RESTART
    assert economy_plan(InterruptReason.STALLED, failures=2, after=2) is Plan.ESCALATE


def test_an_economy_crash_is_resumed_in_economy_first() -> None:
    assert economy_plan(InterruptReason.CRASHED, failures=1) is Plan.RESUME
    assert economy_plan(InterruptReason.CRASHED, failures=3, resumes_used=2) is Plan.ESCALATE


def test_an_economy_start_that_failed_moves_up_at_once() -> None:
    assert economy_plan(InterruptReason.CRASHED, failures=1, has_session=False) is Plan.ESCALATE


def test_what_does_not_count_is_recovered_as_in_premium() -> None:
    plan = economy_plan(InterruptReason.STALLED, failures=0, counts=False)
    assert plan is Plan.RESUME  # a stall with a session, resumes left
    # Limits and sign-ins never move a task up: the account is the same in both tiers.
    assert economy_plan(InterruptReason.RATE_LIMITED, failures=5) is Plan.RESUME
    assert economy_plan(InterruptReason.SIGNED_OUT, failures=5) is Plan.ASK_HUMAN


def test_an_economy_failure_with_no_agent_to_go_on_waits() -> None:
    assert economy_plan(InterruptReason.STALLED, failures=1, available=False) is Plan.WAIT
