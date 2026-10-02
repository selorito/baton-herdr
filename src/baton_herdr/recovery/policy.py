"""What to do about what an agent's pane shows. Pure; the runner acts on the answer.

v1 policy, deliberately small (roadmap: "policy engine" deepens it):

- a usage limit, a crash, a stall or a full context interrupts the attempt;
  ``plan_recovery`` then decides whether to resume it, hand the task to another
  agent, wait, or ask a person;
- a blocker that needs a person stops the attempt and asks the operator;
- an agent that worked and is idle again has finished its turn.
"""

from __future__ import annotations

from enum import StrEnum

from baton_herdr.core.model import AgentState, InterruptReason


class Verdict(StrEnum):
    CONTINUE = "continue"
    TURN_FINISHED = "turn_finished"
    INTERRUPT = "interrupt"
    NEEDS_HUMAN = "needs_human"


_INTERRUPTS = {
    AgentState.RATE_LIMITED: InterruptReason.RATE_LIMITED,
    AgentState.CONTEXT_FULL: InterruptReason.CONTEXT_FULL,
    AgentState.CRASHED: InterruptReason.CRASHED,
}


def assess(state: AgentState, *, worked: bool) -> Verdict:
    """Judge one classified observation of an attempt that has been given its prompt.

    ``worked`` is whether the attempt has been seen working since the prompt.
    """
    if state in _INTERRUPTS:
        return Verdict.INTERRUPT
    if state.needs_human:
        return Verdict.NEEDS_HUMAN
    if state is AgentState.IDLE and worked:
        return Verdict.TURN_FINISHED
    return Verdict.CONTINUE


def interrupt_reason(state: AgentState) -> InterruptReason:
    """The reason recorded for an ``INTERRUPT`` verdict."""
    return _INTERRUPTS[state]


class Plan(StrEnum):
    """What to do with an attempt that was interrupted."""

    RESUME = "resume"  # continue the same agent session in a fresh pane
    HAND_OFF = "hand_off"  # end the attempt; start a new one on another available agent
    RESTART = "restart"  # end the attempt; start a fresh session, the same agent included
    WAIT = "wait"  # nothing can run now; try again later
    ASK_HUMAN = "ask_human"


_RESUMABLE_FAILURES = frozenset({InterruptReason.CRASHED, InterruptReason.STALLED})


def plan_recovery(  # noqa: PLR0913 - each input is a separate fact about the situation
    reason: InterruptReason,
    *,
    has_session: bool,
    agent_available: bool,
    another_agent_available: bool,
    failure_resumes_used: int,
    max_failure_resumes: int,
) -> Plan:
    """Decide what happens to an interrupted attempt.

    - A usage limit is resumed in the same session once the agent is available
      again, which keeps the conversation; before that the task moves to another
      available agent, or waits.
    - A crash or a stall is resumed in the same session a limited number of times
      (ADR 0005); after that, or without a session to resume, a person decides.
    - A session the agent can no longer reopen is restarted as a fresh session.
    - A full context, or an operator's interruption, always goes to a person.
    """
    can_resume = has_session and agent_available
    someone_available = agent_available or another_agent_available
    if reason is InterruptReason.RATE_LIMITED:
        if can_resume:
            return Plan.RESUME
        return Plan.HAND_OFF if someone_available else Plan.WAIT
    if reason is InterruptReason.RESUME_FAILED:
        # The session is gone; its work is in the working directory. Start afresh.
        return Plan.RESTART if someone_available else Plan.WAIT
    if reason in _RESUMABLE_FAILURES and can_resume and failure_resumes_used < max_failure_resumes:
        return Plan.RESUME
    return Plan.ASK_HUMAN
