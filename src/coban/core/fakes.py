"""In-memory implementations of the ``core`` protocols, for tests and dry runs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from coban.core.events import Event, StoredEvent
from coban.core.ports import ConcurrencyError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from coban.core.model import TaskId


class InMemoryEventStore:
    def __init__(self) -> None:
        self._events: list[StoredEvent] = []

    async def append(
        self, events: Sequence[Event], *, expected_seq: int | None = None
    ) -> Sequence[StoredEvent]:
        current = len(self._events)
        if expected_seq is not None and expected_seq != current:
            raise ConcurrencyError(expected_seq=expected_seq, actual_seq=current)
        stored = [
            StoredEvent(seq=current + offset, event=event)
            for offset, event in enumerate(events, start=1)
        ]
        self._events.extend(stored)
        return stored

    async def read(
        self, *, after_seq: int = 0, task_id: TaskId | None = None
    ) -> Sequence[StoredEvent]:
        return [
            stored
            for stored in self._events[max(after_seq, 0) :]
            if task_id is None or stored.event.task_id == task_id
        ]

    async def last_seq(self) -> int:
        return len(self._events)


class FixedClock:
    """A clock that only moves when told to."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 1, 1, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta
