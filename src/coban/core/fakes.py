"""In-memory implementations of the ``core`` protocols, for tests and dry runs."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from coban.core.events import Event, StoredEvent
from coban.core.model import AgentState
from coban.core.panes import (
    AgentBlockedError,
    AgentNotRunningError,
    PaneNotFoundError,
    PaneObservation,
    PaneProcess,
)
from coban.core.ports import ConcurrencyError, DuplicateEventError

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Sequence

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
        known = {stored.event.event_id for stored in self._events}
        new_ids = [event.event_id for event in events]
        if known.intersection(new_ids) or len(set(new_ids)) != len(new_ids):
            raise DuplicateEventError
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


class FakePaneHost:
    """Scriptable pane host. Tests set observations and screens, then inspect ``sent``."""

    def __init__(self) -> None:
        self.observations: dict[str, PaneObservation] = {}
        self.screens: dict[str, str] = {}
        self.process_lists: dict[str, list[PaneProcess]] = {}
        self.sent: list[tuple[str, str, object]] = []
        self._watchers: dict[str, list[asyncio.Queue[PaneObservation]]] = {}
        self._next_pane = 1

    def set_observation(self, observation: PaneObservation) -> None:
        self.observations[observation.pane_id] = observation
        for queue in self._watchers.get(observation.pane_id, []):
            queue.put_nowait(observation)

    async def observe(self, pane_id: str) -> PaneObservation:
        return self._known(pane_id)

    async def watch(self, pane_id: str) -> AsyncGenerator[PaneObservation]:
        queue: asyncio.Queue[PaneObservation] = asyncio.Queue()
        queue.put_nowait(self._known(pane_id))
        self._watchers.setdefault(pane_id, []).append(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            self._watchers[pane_id].remove(queue)

    async def read_screen(self, pane_id: str, *, lines: int = 200) -> str:
        self._known(pane_id)
        rows = self.screens.get(pane_id, "").splitlines(keepends=True)
        return "".join(rows[-lines:])

    async def send_prompt(self, pane_id: str, text: str) -> None:
        observation = self._known(pane_id)
        if observation.agent is None:
            raise AgentNotRunningError(f"no agent in {pane_id}", code="agent_not_found")
        if observation.state.needs_human:
            raise AgentBlockedError(f"agent in {pane_id} is blocked", code="agent_blocked")
        self.sent.append((pane_id, "prompt", text))

    async def send_text(self, pane_id: str, text: str) -> None:
        self._known(pane_id)
        self.sent.append((pane_id, "text", text))

    async def send_keys(self, pane_id: str, keys: Sequence[str]) -> None:
        self._known(pane_id)
        self.sent.append((pane_id, "keys", tuple(keys)))

    async def open_pane(self, *, cwd: str, label: str | None = None) -> str:
        pane_id = f"fake:p{self._next_pane}"
        self._next_pane += 1
        self.observations[pane_id] = PaneObservation(
            pane_id=pane_id, agent=None, state=AgentState.UNKNOWN, evidence="fake:new", cwd=cwd
        )
        self.sent.append((pane_id, "open", label))
        return pane_id

    async def close_pane(self, pane_id: str) -> None:
        self._known(pane_id)
        del self.observations[pane_id]

    async def find_session(self, session_ref: str) -> str | None:
        for pane_id, observation in self.observations.items():
            if observation.session_ref == session_ref:
                return pane_id
        return None

    async def processes(self, pane_id: str) -> Sequence[PaneProcess]:
        self._known(pane_id)
        return list(self.process_lists.get(pane_id, []))

    def _known(self, pane_id: str) -> PaneObservation:
        try:
            return self.observations[pane_id]
        except KeyError:
            raise PaneNotFoundError(f"pane {pane_id} not found", code="pane_not_found") from None
