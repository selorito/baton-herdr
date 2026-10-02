"""The decision half of supervising one attempt: pure, so it can be tested as a table.

Given what the agent shows now and what has happened so far, ``next_step`` says
what the runner should do. The runner only gathers observations and carries the
step out.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import TYPE_CHECKING

from coban.core.events import (
    AgentStateObserved,
    AttemptPrompted,
    AttemptResumed,
    AttemptStarted,
    OperatorActed,
)
from coban.core.model import AgentState, OperatorAction
from coban.recovery.policy import Verdict, assess, interrupt_reason

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from coban.core.events import StoredEvent
    from coban.core.model import AttemptId, InterruptReason

# More answers than this to start-up dialogs in one attempt means something is looping.
MAX_STARTUP_ANSWERS = 3


class Action(StrEnum):
    WAIT = "wait"
    SEND_PROMPT = "send_prompt"
    ANSWER_STARTUP = "answer_startup"
    FINISH = "finish"
    ASK_HUMAN = "ask_human"
    INTERRUPT = "interrupt"
    LOST_TARGET = "lost_target"  # ready for the prompt, but the pane cannot be verified


@dataclass(frozen=True, slots=True)
class TurnState:
    prompt_sent: bool = False
    worked: bool = False
    startup_answers_left: int = MAX_STARTUP_ANSWERS
    # The operator denied a permission in this turn and has not said what to do instead.
    denied: bool = False


@dataclass(frozen=True, slots=True)
class Step:
    action: Action
    keys: tuple[str, ...] = ()
    reason: InterruptReason | None = None


def next_step(
    turn: TurnState,
    state: AgentState,
    *,
    startup_keys: Sequence[str] | None,
    can_send: bool,
) -> tuple[TurnState, Step]:
    """Decide the next step for an attempt whose agent now shows ``state``.

    ``startup_keys`` is the adapter's safe answer to the dialog on screen, if any;
    ``can_send`` is whether the pane was verified to host the attempt (ADR 0006).
    """
    if not turn.prompt_sent:
        if state is AgentState.IDLE:
            if not can_send:
                return turn, Step(Action.LOST_TARGET)
            return replace(turn, prompt_sent=True), Step(Action.SEND_PROMPT)
        if (
            state is AgentState.BLOCKED_OTHER
            and startup_keys
            and turn.startup_answers_left > 0
            and can_send
        ):
            answered = replace(turn, startup_answers_left=turn.startup_answers_left - 1)
            return answered, Step(Action.ANSWER_STARTUP, keys=tuple(startup_keys))
        verdict = assess(state, worked=False)
    else:
        turn = replace(turn, worked=turn.worked or state is AgentState.WORKING)
        verdict = assess(state, worked=turn.worked)

    if verdict is Verdict.INTERRUPT:
        return turn, Step(Action.INTERRUPT, reason=interrupt_reason(state))
    return turn, Step(_VERDICT_ACTIONS[verdict])


_VERDICT_ACTIONS = {
    Verdict.CONTINUE: Action.WAIT,
    Verdict.TURN_FINISHED: Action.FINISH,
    Verdict.NEEDS_HUMAN: Action.ASK_HUMAN,
}


def turn_from_log(events: Iterable[StoredEvent], attempt_id: AttemptId) -> TurnState:
    """Where the attempt's current turn stands, from the event log.

    The turn starts at the attempt's start or its latest resume. The prompt was
    sent if an ``attempt.prompted`` follows; the agent worked if a ``working``
    observation follows that. A denial by the operator holds until they answer.
    """
    prompt_sent = worked = denied = False
    for stored in events:
        event = stored.event
        if getattr(event, "attempt_id", None) != attempt_id:
            continue
        if isinstance(event, AttemptStarted | AttemptResumed):
            prompt_sent = worked = denied = False
        elif isinstance(event, AttemptPrompted):
            prompt_sent, worked, denied = True, False, False
        elif isinstance(event, OperatorActed):
            denied = event.action is OperatorAction.DENY
        elif isinstance(event, AgentStateObserved) and prompt_sent:
            worked = worked or event.state is AgentState.WORKING
    return TurnState(prompt_sent=prompt_sent, worked=worked, denied=denied)
