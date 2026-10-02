from __future__ import annotations

import pytest

from coban.core.model import AgentState, InterruptReason
from coban.scheduler.turn import MAX_STARTUP_ANSWERS, Action, TurnState, next_step

FRESH = TurnState()
SENT = TurnState(prompt_sent=True)
WORKED = TurnState(prompt_sent=True, worked=True)
UPDATE_KEYS = ("Down", "Enter")


@pytest.mark.parametrize(
    ("turn", "state", "can_send", "keys", "action"),
    [
        (FRESH, AgentState.UNKNOWN, True, None, Action.WAIT),
        (FRESH, AgentState.IDLE, True, None, Action.SEND_PROMPT),
        (FRESH, AgentState.IDLE, False, None, Action.LOST_TARGET),
        (FRESH, AgentState.BLOCKED_OTHER, True, UPDATE_KEYS, Action.ANSWER_STARTUP),
        # A dialog without a safe answer, or a pane that cannot be verified: a person decides.
        (FRESH, AgentState.BLOCKED_OTHER, True, None, Action.ASK_HUMAN),
        (FRESH, AgentState.BLOCKED_OTHER, False, UPDATE_KEYS, Action.ASK_HUMAN),
        (FRESH, AgentState.RATE_LIMITED, True, None, Action.INTERRUPT),
        (SENT, AgentState.IDLE, True, None, Action.WAIT),  # not picked up yet
        (SENT, AgentState.WORKING, True, None, Action.WAIT),
        (WORKED, AgentState.IDLE, True, None, Action.FINISH),
        (WORKED, AgentState.BLOCKED_PERMISSION, True, None, Action.ASK_HUMAN),
        (WORKED, AgentState.CRASHED, False, None, Action.INTERRUPT),
        # Once the prompt is out, start-up answers no longer apply.
        (SENT, AgentState.BLOCKED_OTHER, True, UPDATE_KEYS, Action.ASK_HUMAN),
    ],
)
def test_next_step(
    turn: TurnState,
    state: AgentState,
    can_send: bool,
    keys: tuple[str, ...] | None,
    action: Action,
) -> None:
    _, step = next_step(turn, state, startup_keys=keys, can_send=can_send)
    assert step.action is action


def test_a_full_turn_sends_once_notices_work_and_finishes() -> None:
    turn, step = next_step(FRESH, AgentState.IDLE, startup_keys=None, can_send=True)
    assert (step.action, turn.prompt_sent) == (Action.SEND_PROMPT, True)
    turn, step = next_step(turn, AgentState.WORKING, startup_keys=None, can_send=True)
    assert (step.action, turn.worked) == (Action.WAIT, True)
    turn, step = next_step(turn, AgentState.IDLE, startup_keys=None, can_send=True)
    assert step.action is Action.FINISH


def test_interruptions_carry_their_reason() -> None:
    _, step = next_step(WORKED, AgentState.CONTEXT_FULL, startup_keys=None, can_send=True)
    assert (step.action, step.reason) == (Action.INTERRUPT, InterruptReason.CONTEXT_FULL)


def test_start_up_answers_are_capped() -> None:
    turn = FRESH
    actions = []
    for _ in range(MAX_STARTUP_ANSWERS + 1):
        turn, step = next_step(
            turn, AgentState.BLOCKED_OTHER, startup_keys=UPDATE_KEYS, can_send=True
        )
        actions.append(step.action)
    assert actions == [Action.ANSWER_STARTUP] * MAX_STARTUP_ANSWERS + [Action.ASK_HUMAN]
