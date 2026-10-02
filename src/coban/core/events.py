"""Domain events: the only things written to the event log (ADR 0002).

Events are immutable facts in the past tense. Each event type has a stable
``type`` string, which is the discriminator used when events are stored as JSON.
Adding a field needs a default; changing the meaning of a field needs a new
event type.

A task can only be closed (completed, failed, cancelled) when it has no live
attempt. Use :mod:`coban.core.commands` to close a task: it emits the
``attempt.ended`` event first, in the same batch.
"""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    PositiveInt,
    TypeAdapter,
    model_validator,
)

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


class TaskFailed(_Event):
    type: Literal["task.failed"] = "task.failed"
    reason: str | None = None


class TaskCancelled(_Event):
    type: Literal["task.cancelled"] = "task.cancelled"
    reason: str | None = None


class _AttemptEvent(_Event):
    attempt_id: AttemptId


class AttemptStarted(_AttemptEvent):
    type: Literal["attempt.started"] = "attempt.started"
    agent: AgentKind


class AttemptLocated(_AttemptEvent):
    """Where the attempt can be reached now (ADR 0006).

    ``pane_id`` changes when the agent is restarted in another pane;
    ``session_ref`` is the agent's own session id, which arrives after start (a
    hook reports it), may change, or may never be known. A field left ``None``
    keeps its previous value.
    """

    type: Literal["attempt.located"] = "attempt.located"
    pane_id: str | None = None
    session_ref: str | None = None

    @model_validator(mode="after")
    def _says_something(self) -> AttemptLocated:
        if self.pane_id is None and self.session_ref is None:
            msg = "attempt.located needs a pane_id or a session_ref"
            raise ValueError(msg)
        return self


class AgentStateObserved(_AttemptEvent):
    type: Literal["attempt.state_observed"] = "attempt.state_observed"
    state: AgentState
    source: ObservationSource
    # What the observation is based on, e.g. a herdr rule id or a detector rule.
    evidence: str | None = None


class AttemptPrompted(_AttemptEvent):
    """coban submitted a prompt to the attempt's agent.

    The text is not stored; ``kind`` says which prompt it was and ``chars`` how
    long. Re-attaching after a restart uses this to avoid sending a prompt twice.
    """

    type: Literal["attempt.prompted"] = "attempt.prompted"
    kind: Literal["task", "handoff", "continue"]
    chars: int = Field(ge=0)


class AttemptInterrupted(_AttemptEvent):
    type: Literal["attempt.interrupted"] = "attempt.interrupted"
    reason: InterruptReason
    resume_not_before: AwareDatetime | None = None


class AttemptResumed(_AttemptEvent):
    type: Literal["attempt.resumed"] = "attempt.resumed"


class AttemptEnded(_AttemptEvent):
    type: Literal["attempt.ended"] = "attempt.ended"
    outcome: AttemptOutcome


type Event = Annotated[
    TaskCreated
    | TaskCompleted
    | TaskFailed
    | TaskCancelled
    | AttemptStarted
    | AttemptLocated
    | AgentStateObserved
    | AttemptPrompted
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
