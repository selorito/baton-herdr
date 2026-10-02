"""What to do about what an agent's pane shows. Pure; the runner acts on the answer.

v1 policy, deliberately small (roadmap: "policy engine" deepens it):

- a usage limit interrupts the attempt; the task moves to another available
  agent, or waits for the reset when there is none;
- a full context, a crash or a blocker that needs a person stops the attempt and
  asks the operator;
- an agent that worked and is idle again has finished its turn.
"""

from __future__ import annotations

from enum import StrEnum

from coban.core.model import AgentState, InterruptReason


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


def hands_off(reason: InterruptReason) -> bool:
    """Whether another agent may take the task over after this interruption.

    Only a usage limit says nothing about the task itself; after a crash or a full
    context a person should look first.
    """
    return reason is InterruptReason.RATE_LIMITED
