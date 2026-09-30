from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from coban.core.events import TaskCreated
from coban.core.fakes import FixedClock, InMemoryEventStore
from coban.core.model import TaskId
from coban.core.ports import Clock, ConcurrencyError, EventStore

NOW = datetime(2026, 9, 30, tzinfo=UTC)


def created(task: str) -> TaskCreated:
    return TaskCreated(
        occurred_at=NOW, task_id=TaskId(task), title=task, instructions="-", workdir="/w"
    )


async def test_append_assigns_contiguous_positions_and_read_filters() -> None:
    store: EventStore = InMemoryEventStore()

    first = await store.append([created("t1"), created("t2")])
    second = await store.append([created("t3")], expected_seq=2)

    assert [s.seq for s in (*first, *second)] == [1, 2, 3]
    assert await store.last_seq() == 3
    assert [s.seq for s in await store.read(after_seq=1)] == [2, 3]
    assert [s.event.task_id for s in await store.read(task_id=TaskId("t2"))] == ["t2"]


async def test_append_with_a_stale_expected_seq_writes_nothing() -> None:
    store = InMemoryEventStore()
    await store.append([created("t1")])

    with pytest.raises(ConcurrencyError) as caught:
        await store.append([created("t2")], expected_seq=0)

    assert (caught.value.expected_seq, caught.value.actual_seq) == (0, 1)
    assert await store.last_seq() == 1


def test_fixed_clock_only_moves_when_advanced() -> None:
    clock: Clock = FixedClock(NOW)
    assert clock.now() == NOW
    assert isinstance(clock, FixedClock)
    clock.advance(timedelta(minutes=5))
    assert clock.now() == NOW + timedelta(minutes=5)
