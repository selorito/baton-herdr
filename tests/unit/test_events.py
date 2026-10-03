from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from baton_herdr.core.events import (
    EVENT_ADAPTER,
    AgentChosen,
    AgentStateObserved,
    AttemptInterrupted,
    AttemptLocated,
    BudgetNote,
    StoredEvent,
    TaskCreated,
)
from baton_herdr.core.model import (
    AgentKind,
    AgentState,
    AttemptId,
    InterruptReason,
    ObservationSource,
    TaskId,
)

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
TASK = TaskId("t1")


def test_event_round_trips_through_json_by_its_type_tag() -> None:
    event = AttemptInterrupted(
        occurred_at=NOW,
        task_id=TASK,
        attempt_id=AttemptId(UUID(int=1)),
        reason=InterruptReason.RATE_LIMITED,
        resume_not_before=NOW,
    )
    raw = EVENT_ADAPTER.dump_json(event)

    assert json.loads(raw)["type"] == "attempt.interrupted"
    assert EVENT_ADAPTER.validate_json(raw) == event


def test_an_agent_choice_keeps_its_reason_and_what_it_was_based_on() -> None:
    event = AgentChosen(
        occurred_at=NOW,
        task_id=TASK,
        agent=None,
        reason="No agent can take the task: claude limited.",
        budgets=(
            BudgetNote(agent=AgentKind.CLAUDE, available=False, remaining_percent=0, estimate=True),
        ),
    )
    raw = EVENT_ADAPTER.dump_json(event)

    assert json.loads(raw)["type"] == "task.agent_chosen"
    assert EVENT_ADAPTER.validate_json(raw) == event
    with pytest.raises(ValidationError):
        AgentChosen(occurred_at=NOW, task_id=TASK, agent=AgentKind.CODEX, reason="")


def test_stored_event_picks_the_event_class_from_the_type_tag() -> None:
    payload = {
        "seq": 3,
        "event": {
            "type": "attempt.state_observed",
            "event_id": "3f2c1a9e-1111-4222-8333-444455556666",
            "occurred_at": "2026-09-30T12:00:00Z",
            "task_id": "t1",
            "attempt_id": "00000000-0000-0000-0000-000000000001",
            "state": "blocked_question",
            "source": "herdr",
            "evidence": "live_blocked_form",
        },
    }
    stored = StoredEvent.model_validate(payload)

    assert isinstance(stored.event, AgentStateObserved)
    assert stored.event.state is AgentState.BLOCKED_QUESTION
    assert stored.event.source is ObservationSource.HERDR


@pytest.mark.parametrize(
    "change",
    [
        {"type": "task.exploded"},  # unknown event type
        {"occurred_at": "2026-09-30T12:00:00"},  # naive datetime
        {"surprise": 1},  # unknown field
    ],
)
def test_invalid_events_are_rejected(change: dict[str, object]) -> None:
    payload = {
        "type": "task.created",
        "occurred_at": "2026-09-30T12:00:00Z",
        "task_id": "t1",
        "title": "x",
        "instructions": "y",
        "workdir": "/w",
    }
    with pytest.raises(ValidationError):
        EVENT_ADAPTER.validate_python(payload | change)


def test_events_are_immutable() -> None:
    event = TaskCreated(occurred_at=NOW, task_id=TASK, title="x", instructions="y", workdir="/w")
    with pytest.raises(ValidationError):
        event.title = "changed"  # type: ignore[misc]


def test_attempt_ids_must_be_uuids() -> None:
    payload = {
        "type": "attempt.started",
        "occurred_at": "2026-09-30T12:00:00Z",
        "task_id": "t1",
        "attempt_id": "w1:p6",  # a pane id is a location, not an identity
        "agent": "claude",
    }
    with pytest.raises(ValidationError):
        EVENT_ADAPTER.validate_python(payload)


def test_a_location_event_must_carry_a_pane_or_a_session() -> None:
    with pytest.raises(ValidationError, match="needs a pane_id or a session_ref"):
        AttemptLocated(occurred_at=NOW, task_id=TASK, attempt_id=AttemptId(UUID(int=1)))
