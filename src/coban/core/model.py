"""Identifiers and enumerations shared by the whole domain."""

from __future__ import annotations

from enum import StrEnum
from typing import NewType

TaskId = NewType("TaskId", str)
AttemptId = NewType("AttemptId", str)


class AgentKind(StrEnum):
    CLAUDE = "claude"
    CODEX = "codex"
    GEMINI = "gemini"
    OPENCODE = "opencode"


class AgentState(StrEnum):
    """What an agent is doing, as coban understands it (ADR 0004)."""

    WORKING = "working"
    IDLE = "idle"
    BLOCKED_PERMISSION = "blocked_permission"
    BLOCKED_QUESTION = "blocked_question"
    BLOCKED_OTHER = "blocked_other"
    RATE_LIMITED = "rate_limited"
    CONTEXT_FULL = "context_full"
    CRASHED = "crashed"
    UNKNOWN = "unknown"

    @property
    def needs_human(self) -> bool:
        return self in _NEEDS_HUMAN


_NEEDS_HUMAN = frozenset(
    {AgentState.BLOCKED_PERMISSION, AgentState.BLOCKED_QUESTION, AgentState.BLOCKED_OTHER}
)


class ObservationSource(StrEnum):
    """Who produced a state observation."""

    HERDR = "herdr"
    DETECTOR = "detector"
    ADAPTER = "adapter"
    OPERATOR = "operator"


class InterruptReason(StrEnum):
    RATE_LIMITED = "rate_limited"
    CONTEXT_FULL = "context_full"
    CRASHED = "crashed"
    STALLED = "stalled"
    OPERATOR = "operator"


class AttemptOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ABANDONED = "abandoned"


class AttemptStatus(StrEnum):
    ACTIVE = "active"
    INTERRUPTED = "interrupted"
    ENDED = "ended"


class TaskStatus(StrEnum):
    PENDING = "pending"  # no live attempt; can be scheduled
    RUNNING = "running"  # an attempt is active
    NEEDS_HUMAN = "needs_human"  # the active attempt waits for a person
    WAITING = "waiting"  # the attempt is interrupted and may be resumed
    COMPLETED = "completed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in {TaskStatus.COMPLETED, TaskStatus.CANCELLED}
