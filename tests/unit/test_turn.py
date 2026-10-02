from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from baton_herdr.core.events import (
    AgentStateObserved,
    AttemptPrompted,
    AttemptResumed,
    AttemptStarted,
    Event,
    OperatorActed,
    StoredEvent,
)
from baton_herdr.core.model import (
    AgentKind,
    AgentState,
    AttemptId,
    InterruptReason,
    ObservationSource,
    OperatorAction,
    TaskId,
)
from baton_herdr.scheduler.turn import (
    MAX_STARTUP_ANSWERS,
    Action,
    TurnState,
    next_step,
    turn_from_log,
)

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


NOW = datetime(2026, 10, 3, tzinfo=UTC)
TASK = TaskId("t")
A1, OTHER = AttemptId(UUID(int=1)), AttemptId(UUID(int=2))


def _seen(state: AgentState, attempt: AttemptId = A1) -> AgentStateObserved:
    return AgentStateObserved(
        occurred_at=NOW,
        task_id=TASK,
        attempt_id=attempt,
        state=state,
        source=ObservationSource.HERDR,
    )


def _log(*events: Event) -> list[StoredEvent]:
    return [StoredEvent(seq=i, event=e) for i, e in enumerate(events, start=1)]


def test_turn_from_log_starts_over_at_each_start_or_resume() -> None:
    started = AttemptStarted(occurred_at=NOW, task_id=TASK, attempt_id=A1, agent=AgentKind.CLAUDE)
    prompted = AttemptPrompted(occurred_at=NOW, task_id=TASK, attempt_id=A1, kind="task", chars=10)
    resumed = AttemptResumed(occurred_at=NOW, task_id=TASK, attempt_id=A1)

    assert turn_from_log(_log(started, _seen(AgentState.IDLE)), A1) == TurnState()
    assert turn_from_log(_log(started, _seen(AgentState.WORKING), prompted), A1) == TurnState(
        prompt_sent=True
    )
    assert turn_from_log(
        _log(started, prompted, _seen(AgentState.WORKING), _seen(AgentState.IDLE)), A1
    ) == TurnState(prompt_sent=True, worked=True)
    # Another attempt's work does not count; a resume starts a new turn.
    assert (
        turn_from_log(_log(started, prompted, _seen(AgentState.WORKING, OTHER)), A1).worked is False
    )
    assert (
        turn_from_log(_log(started, prompted, _seen(AgentState.WORKING), resumed), A1)
        == TurnState()
    )


def test_a_denial_holds_until_the_operator_answers() -> None:
    def acted(action: OperatorAction) -> OperatorActed:
        return OperatorActed(
            occurred_at=NOW, task_id=TASK, attempt_id=A1, action=action, blocker_seq=3, by="cli"
        )

    started = AttemptStarted(occurred_at=NOW, task_id=TASK, attempt_id=A1, agent=AgentKind.CLAUDE)
    prompted = AttemptPrompted(occurred_at=NOW, task_id=TASK, attempt_id=A1, kind="task", chars=10)
    denied = _log(started, prompted, acted(OperatorAction.DENY))
    assert turn_from_log(denied, A1).denied is True
    answered = _log(started, prompted, acted(OperatorAction.DENY), acted(OperatorAction.ANSWER))
    assert turn_from_log(answered, A1).denied is False
