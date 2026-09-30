"""One contract, run against every EventStore implementation."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID

import pytest

from coban.core.events import AttemptStarted, Event, StoredEvent, TaskCreated
from coban.core.fakes import FixedClock, InMemoryEventStore
from coban.core.model import AgentKind, AttemptId, TaskId
from coban.core.ports import Clock, ConcurrencyError, DuplicateEventError, EventStore
from coban.core.projection import project
from coban.ledger import open_event_store

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence
    from pathlib import Path

NOW = datetime(2026, 9, 30, tzinfo=UTC)


def created(task: str, event_id: UUID | None = None) -> TaskCreated:
    extra = {} if event_id is None else {"event_id": event_id}
    return TaskCreated.model_validate(
        {"occurred_at": NOW, "task_id": task, "title": task, "instructions": "-", "workdir": "/w"}
        | extra
    )


@pytest.fixture(params=["memory", "sqlite"])
async def store(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[EventStore]:
    if request.param == "memory":
        yield InMemoryEventStore()
        return
    sqlite_store = await open_event_store(tmp_path / "data" / "coban.db")
    yield sqlite_store
    await sqlite_store.close()


async def test_empty_log(store: EventStore) -> None:
    assert await store.last_seq() == 0
    assert await store.read() == []
    assert await store.append([]) == []


async def test_append_assigns_contiguous_positions_and_read_filters(store: EventStore) -> None:
    first = await store.append([created("t1"), created("t2")])
    second = await store.append([created("t3")], expected_seq=2)

    assert [s.seq for s in (*first, *second)] == [1, 2, 3]
    assert await store.last_seq() == 3
    assert [s.seq for s in await store.read(after_seq=1)] == [2, 3]
    assert [s.event.task_id for s in await store.read(task_id=TaskId("t2"))] == ["t2"]


async def test_events_come_back_equal_to_what_was_appended(store: EventStore) -> None:
    events: list[Event] = [
        created("t1"),
        AttemptStarted(
            occurred_at=NOW + timedelta(seconds=1),
            task_id=TaskId("t1"),
            attempt_id=AttemptId("a1"),
            agent=AgentKind.CODEX,
            pane_id="w1:p6",
            session_ref="01a0f150-e774-7033-83aa-cc01d8ac0577",
        ),
    ]
    appended = await store.append(events)

    assert await store.read() == list(appended)
    assert [s.event for s in appended] == events
    assert project(await store.read()).last_seq == 2


async def test_append_with_a_stale_expected_seq_writes_nothing(store: EventStore) -> None:
    await store.append([created("t1")])

    with pytest.raises(ConcurrencyError) as caught:
        await store.append([created("t2")], expected_seq=0)

    assert (caught.value.expected_seq, caught.value.actual_seq) == (0, 1)
    assert await store.last_seq() == 1


async def test_a_duplicate_event_id_rejects_the_whole_batch(store: EventStore) -> None:
    same = UUID("3f2c1a9e-1111-4222-8333-444455556666")
    await store.append([created("t1", same)])

    with pytest.raises(DuplicateEventError):
        await store.append([created("t2"), created("t3", same)])

    assert await store.last_seq() == 1


async def test_racing_appends_with_the_same_expected_seq_let_exactly_one_win(
    store: EventStore,
) -> None:
    async def attempt(task: str) -> Sequence[StoredEvent] | ConcurrencyError:
        try:
            return await store.append([created(task)], expected_seq=0)
        except ConcurrencyError as err:
            return err

    results = await asyncio.gather(*(attempt(f"t{i}") for i in range(8)))

    winners = [r for r in results if not isinstance(r, ConcurrencyError)]
    assert len(winners) == 1
    assert await store.last_seq() == 1


def test_fixed_clock_only_moves_when_advanced() -> None:
    clock: Clock = FixedClock(NOW)
    assert clock.now() == NOW
    assert isinstance(clock, FixedClock)
    clock.advance(timedelta(minutes=5))
    assert clock.now() == NOW + timedelta(minutes=5)
