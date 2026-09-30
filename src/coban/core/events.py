"""Domain events: the only things written to the event log (ADR 0002).

Events are immutable facts in the past tense. Each event type has a stable
``type`` string, which is the discriminator used when events are stored as JSON.
Adding a field needs a default; changing the meaning of a field needs a new
event type.
"""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, PositiveInt, TypeAdapter

from coban.core.model import (
    AgentKind,
    AgentState,
    AttemptId,
    AttemptOutcome,
    InterruptReason,
    ObservationSource,
    TaskId,
)


class _Event(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: UUID = Field(default_factory=uuid4)
    occurred_at: AwareDatetime
    task_id: TaskId


class TaskCreated(_Event):
    type: Literal["task.created"] = "task.created"
    title: str
    instructions: str
    workdir: str


class TaskCompleted(_Event):
    type: Literal["task.completed"] = "task.completed"


class TaskCancelled(_Event):
    type: Literal["task.cancelled"] = "task.cancelled"
    reason: str | None = None


class _AttemptEvent(_Event):
    attempt_id: AttemptId


class AttemptStarted(_AttemptEvent):
    type: Literal["attempt.started"] = "attempt.started"
    agent: AgentKind
    pane_id: str | None = None
    session_ref: str | None = None


class AgentStateObserved(_AttemptEvent):
    type: Literal["attempt.state_observed"] = "attempt.state_observed"
    state: AgentState
    source: ObservationSource
    # What the observation is based on, e.g. a herdr rule id or a detector rule.
    evidence: str | None = None


class AttemptInterrupted(_AttemptEvent):
    type: Literal["attempt.interrupted"] = "attempt.interrupted"
    reason: InterruptReason
    resume_not_before: AwareDatetime | None = None


class AttemptResumed(_AttemptEvent):
    type: Literal["attempt.resumed"] = "attempt.resumed"
    pane_id: str | None = None


class AttemptEnded(_AttemptEvent):
    type: Literal["attempt.ended"] = "attempt.ended"
    outcome: AttemptOutcome


type Event = Annotated[
    TaskCreated
    | TaskCompleted
    | TaskCancelled
    | AttemptStarted
    | AgentStateObserved
    | AttemptInterrupted
    | AttemptResumed
    | AttemptEnded,
    Field(discriminator="type"),
]

EVENT_ADAPTER: TypeAdapter[Event] = TypeAdapter(Event)


class StoredEvent(BaseModel):
    """An event with the position the log assigned to it. ``seq`` starts at 1."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: PositiveInt
    event: Event
