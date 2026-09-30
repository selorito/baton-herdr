"""Protocols for everything ``core`` needs from the outside world.

Real implementations live in the outer packages (``ledger`` for the event
store); tests and dry runs use the fakes in :mod:`coban.core.fakes`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from coban.core.events import Event, StoredEvent
    from coban.core.model import TaskId


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
