from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from coban.core.events import (
    EVENT_ADAPTER,
    AgentStateObserved,
    AttemptInterrupted,
    StoredEvent,
    TaskCreated,
)
from coban.core.model import AgentState, AttemptId, InterruptReason, ObservationSource, TaskId

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
TASK = TaskId("t1")


def test_event_round_trips_through_json_by_its_type_tag() -> None:
    event = AttemptInterrupted(
        occurred_at=NOW,
        task_id=TASK,
        attempt_id=AttemptId("a1"),
        reason=InterruptReason.RATE_LIMITED,
        resume_not_before=NOW,
    )
    raw = EVENT_ADAPTER.dump_json(event)

    assert json.loads(raw)["type"] == "attempt.interrupted"
    assert EVENT_ADAPTER.validate_json(raw) == event


def test_stored_event_picks_the_event_class_from_the_type_tag() -> None:
    payload = {
        "seq": 3,
        "event": {
            "type": "attempt.state_observed",
            "event_id": "3f2c1a9e-1111-4222-8333-444455556666",
            "occurred_at": "2026-09-30T12:00:00Z",
            "task_id": "t1",
            "attempt_id": "a1",
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
