"""Domain events: the only things written to the event log (ADR 0002).

Events are immutable facts in the past tense. Each event type has a stable
``type`` string, which is the discriminator used when events are stored as JSON.
Adding a field needs a default; changing the meaning of a field needs a new
event type.

A task can only be closed (completed, failed, cancelled) when it has no live
attempt. Use :mod:`baton_herdr.core.commands` to close a task: it emits the
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

from baton_herdr.core.model import (
    AgentKind,
    AgentState,
    AttemptId,
    AttemptOutcome,
    InterruptReason,
    ObservationSource,
    OperatorAction,
    PermissionDecision,
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
    # A task of a project (ADR 0016): its plan, one of its steps, or the merge of a step
    # whose branch conflicted (ADR 0017). A task with ``after`` waits until that task has
    # completed.
    project: str | None = None
    role: Literal["plan", "step", "merge"] | None = None
    after: TaskId | None = None
    # A step of a parallel plan (ADR 0017): the steps it builds on, each completed and
    # merged before it starts, and the branch of its own worktree, its ``workdir``.
    needs: tuple[TaskId, ...] = ()
    branch: str | None = None
    # On a plan task: the request as the operator wrote it.
    request: str | None = Field(default=None, max_length=4000)


class TaskCompleted(_Event):
    type: Literal["task.completed"] = "task.completed"


class TaskFailed(_Event):
    type: Literal["task.failed"] = "task.failed"
    reason: str | None = None


class TaskCancelled(_Event):
    type: Literal["task.cancelled"] = "task.cancelled"
    reason: str | None = None


class PlanStep(BaseModel):
    """One step of a project's plan (ADR 0016)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str = Field(min_length=1, max_length=120)
    # The planner is asked for at most STEP_CHARS (projects.plan); this leaves room for an
    # overshoot, which rejected a whole plan when the bound was 4000 (seen live).
    instructions: str = Field(min_length=1, max_length=12000)
    # The numbers (from 1) of the earlier steps this one builds on (ADR 0017); None: the
    # step before it, so a plan without them runs in order.
    needs: tuple[PositiveInt, ...] | None = None


class PlanProposed(_Event):
    """The plan a project's plan task wrote, waiting for the operator (ADR 0016). A note
    about the plan task, which has completed by then."""

    type: Literal["plan.proposed"] = "plan.proposed"
    summary: str = Field(max_length=2000)
    steps: tuple[PlanStep, ...] = Field(min_length=1, max_length=8)


class PlanApproved(_Event):
    type: Literal["plan.approved"] = "plan.approved"
    by: str = Field(min_length=1)  # "telegram:<user id>" or "cli"


class PlanRejected(_Event):
    """The operator cancelled the plan, or baton could not read one."""

    type: Literal["plan.rejected"] = "plan.rejected"
    by: str = Field(min_length=1)
    reason: str | None = Field(default=None, max_length=500)


class ProjectDelivered(_Event):
    """The project's last step completed and its result was sent (ADR 0016). A note about
    that step task."""

    type: Literal["project.delivered"] = "project.delivered"
    project: str
    preview_url: str | None = None


class StepWorktreeOpened(_Event):
    """A parallel step's worktree was made, from the project's ``base`` commit (ADR 0017)."""

    type: Literal["step.worktree_opened"] = "step.worktree_opened"
    path: str
    branch: str
    base: str


class StepMerged(_Event):
    """A completed step's branch is part of the project's history, at ``commit``."""

    type: Literal["step.merged"] = "step.merged"
    commit: str


class StepMergeConflicted(_Event):
    """Merging a completed step's branch conflicted in ``files``; the merge was aborted and
    a merge task was added."""

    type: Literal["step.merge_conflicted"] = "step.merge_conflicted"
    files: tuple[str, ...] = Field(default=(), max_length=200)


class StepMergeFailed(_Event):
    """A conflicted step's merge task completed, but its branch is still not part of the
    project's history; the project stops."""

    type: Literal["step.merge_failed"] = "step.merge_failed"
    reason: str = Field(max_length=500)


# Notes about a task that may follow its completion: they record what happened around the
# task, not to it.
NOTE_TYPES = frozenset(
    {
        "plan.proposed",
        "plan.approved",
        "plan.rejected",
        "project.delivered",
        "step.worktree_opened",
        "step.merged",
        "step.merge_conflicted",
        "step.merge_failed",
    }
)


class BudgetNote(BaseModel):
    """One agent's standing when an agent was chosen (ADR 0012)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    agent: AgentKind
    # Limited by a usage limit it hit (the event log), or free to start.
    available: bool
    # Share of its budget left; None when unknown.
    remaining_percent: float | None = None
    # True when baton derived the figure (Claude); False when the agent reported it.
    estimate: bool = False


class AgentChosen(_Event):
    """The scheduler picked the agent for the task's next attempt, or found none.

    ``reason`` is the decision in words; ``budgets`` what it was based on, for
    every agent that was considered.
    """

    type: Literal["task.agent_chosen"] = "task.agent_chosen"
    agent: AgentKind | None
    reason: str = Field(min_length=1)
    budgets: tuple[BudgetNote, ...] = ()


class _AttemptEvent(_Event):
    attempt_id: AttemptId


class AttemptStarted(_AttemptEvent):
    type: Literal["attempt.started"] = "attempt.started"
    agent: AgentKind
    # The commit checked out in the task's directory when the attempt started, if any;
    # what the task changed is measured from its first attempt's commit.
    workdir_head: str | None = None


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
    """baton submitted a prompt to the attempt's agent.

    The text is not stored; ``kind`` says which prompt it was and ``chars`` how
    long. Re-attaching after a restart uses this to avoid sending a prompt twice.
    """

    type: Literal["attempt.prompted"] = "attempt.prompted"
    # "nudge": the turn ended without the end mark, so the agent was asked to finish.
    kind: Literal["task", "handoff", "continue", "nudge"]
    chars: int = Field(ge=0)


class AttemptInterrupted(_AttemptEvent):
    """The attempt stopped. ``detail`` says what showed it, in words, when there is more
    to say than the reason (for a stall: how long nothing moved, and by which signals)."""

    type: Literal["attempt.interrupted"] = "attempt.interrupted"
    reason: InterruptReason
    resume_not_before: AwareDatetime | None = None
    detail: str | None = Field(default=None, max_length=500)


class AttemptResumed(_AttemptEvent):
    type: Literal["attempt.resumed"] = "attempt.resumed"


class AttemptEnded(_AttemptEvent):
    type: Literal["attempt.ended"] = "attempt.ended"
    outcome: AttemptOutcome


class OperatorActed(_AttemptEvent):
    """The operator acted on a blocker from outside the terminal (ADR 0009).

    ``blocker_seq`` is the ``attempt.state_observed`` event that was answered; each
    blocker is answered at most once. An answer's text is not stored, only its length.
    """

    type: Literal["operator.acted"] = "operator.acted"
    action: OperatorAction
    blocker_seq: PositiveInt
    by: str = Field(min_length=1)  # "telegram:<user id>" or "cli"
    chars: int = Field(default=0, ge=0)


class PermissionDecided(_AttemptEvent):
    """baton's policy decided an agent's permission prompt (ADR 0013).

    ``blocker_seq`` is the ``attempt.state_observed`` event of the prompt; ``prompt`` the
    prompt in one line. ``command`` is what the agent asked to run, or ``None`` for a
    prompt that is not a shell command baton can read. Both are as read from the screen,
    with secrets masked. ``rule`` names the rule that decided, if one did; ``reason`` says
    why in words.
    """

    type: Literal["attempt.permission_decided"] = "attempt.permission_decided"
    blocker_seq: PositiveInt
    decision: PermissionDecision
    prompt: str | None = Field(default=None, max_length=1000)
    command: str | None = Field(default=None, max_length=1000)
    rule: str | None = None
    reason: str = Field(min_length=1)


type Event = Annotated[
    TaskCreated
    | TaskCompleted
    | TaskFailed
    | TaskCancelled
    | AgentChosen
    | AttemptStarted
    | AttemptLocated
    | AgentStateObserved
    | AttemptPrompted
    | OperatorActed
    | PermissionDecided
    | AttemptInterrupted
    | AttemptResumed
    | AttemptEnded
    | PlanProposed
    | PlanApproved
    | PlanRejected
    | ProjectDelivered
    | StepWorktreeOpened
    | StepMerged
    | StepMergeConflicted
    | StepMergeFailed,
    Field(discriminator="type"),
]

EVENT_ADAPTER: TypeAdapter[Event] = TypeAdapter(Event)


class StoredEvent(BaseModel):
    """An event with the position the log assigned to it. ``seq`` starts at 1."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: PositiveInt
    event: Event
