"""Protocols for everything ``core`` needs from the outside world.

Real implementations live in the outer packages (``ledger`` for the event
store); tests and dry runs use the fakes in :mod:`baton_herdr.core.fakes`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from baton_herdr.core.changes import Changes
    from baton_herdr.core.events import Event, StoredEvent
    from baton_herdr.core.model import TaskId
    from baton_herdr.core.usage import RateLimitObservation, UsageEvent, UsageRecord


class ConcurrencyError(Exception):
    """The log moved on since the caller last read it."""

    def __init__(self, *, expected_seq: int, actual_seq: int) -> None:
        self.expected_seq = expected_seq
        self.actual_seq = actual_seq
        super().__init__(f"expected last seq {expected_seq}, log is at {actual_seq}")


class DuplicateEventError(Exception):
    """An event with this ``event_id`` is already in the log."""


class EventStore(Protocol):
    """Append-only, totally ordered event log."""

    async def append(
        self, events: Sequence[Event], *, expected_seq: int | None = None
    ) -> Sequence[StoredEvent]:
        """Append ``events`` atomically and return them with their positions.

        When ``expected_seq`` is given and is not the current last position,
        nothing is written and :class:`ConcurrencyError` is raised. An
        ``event_id`` that is already stored raises :class:`DuplicateEventError`,
        also without writing anything.
        """
        ...

    async def read(
        self, *, after_seq: int = 0, task_id: TaskId | None = None
    ) -> Sequence[StoredEvent]:
        """Return events with ``seq > after_seq`` in order, optionally for one task."""
        ...

    async def last_seq(self) -> int:
        """Position of the newest event, or 0 for an empty log."""
        ...


class Clock(Protocol):
    def now(self) -> datetime:
        """Current time as a timezone-aware UTC datetime."""
        ...


class UsageStore(Protocol):
    """Agent usage telemetry, kept apart from the task event log (ADR 0011).

    Append-only; a record already stored (same record id, or for rate limits the
    same agent, session and time) is skipped, not an error.
    """

    async def record(self, events: Sequence[UsageEvent]) -> int:
        """Store ``events``; return how many were new."""
        ...

    async def latest_at(self) -> datetime | None:
        """The time of the newest stored event: where reading may start again."""
        ...

    async def usage(self, *, since: datetime | None = None) -> Sequence[UsageRecord]:
        """Usage records at or after ``since``, oldest first."""
        ...

    async def rate_limits(self, *, since: datetime | None = None) -> Sequence[RateLimitObservation]:
        """Rate-limit observations at or after ``since``, oldest first."""
        ...


class Workspace(Protocol):
    """The task's working directory, as version control sees it."""

    async def head(self, workdir: str) -> str | None:
        """The commit checked out now, or ``None`` outside a repository."""
        ...

    async def changes(self, workdir: str, since: str | None) -> Changes | None:
        """Files changed since commit ``since`` (``None``: since the last commit),
        committed or not, new files included; ``None`` when it cannot be told."""
        ...


class RepositoryError(Exception):
    """A git operation on a project's repository failed; the message says which."""


@dataclass(frozen=True, slots=True)
class MergeResult:
    """A merge of a step's branch: its commit, or the files that conflicted."""

    commit: str | None = None
    conflicts: tuple[str, ...] = ()

    @property
    def merged(self) -> bool:
        return self.commit is not None


class Repository(Protocol):
    """A project's git repository, written to: its steps' worktrees and their merges
    (ADR 0017). Only directories baton made for a project are written to. Failures raise
    :class:`RepositoryError`."""

    async def commit_all(self, workdir: str, message: str) -> str:
        """Commit everything in ``workdir``, new files included; the commit checked out
        afterwards (the same as before when there was nothing to commit)."""
        ...

    async def add_worktree(self, repo: str, path: str, branch: str) -> str:
        """A new worktree at ``path`` on a new ``branch`` from ``repo``'s HEAD; that commit."""
        ...

    async def merge(self, repo: str, branch: str, message: str) -> MergeResult:
        """Merge ``branch`` into ``repo``'s checked-out branch. A merge that conflicts is
        aborted, leaving ``repo`` as it was, and its conflicts are returned."""
        ...

    async def finish_merge(self, repo: str, message: str) -> None:
        """Commit a merge someone else resolved in ``repo``, if one is under way; a merge
        with files still in conflict is not committed, and raises."""
        ...

    async def contains(self, repo: str, branch: str) -> bool:
        """Whether ``branch`` is part of the history of ``repo``'s HEAD."""
        ...

    async def remove_worktree(self, repo: str, path: str) -> None:
        """Remove the worktree at ``path``; its branch stays."""
        ...
