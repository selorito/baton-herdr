"""The usage tables (ADR 0011): append-only, duplicates skipped by the database."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from baton_herdr.core.events import TaskCreated
from baton_herdr.core.fakes import InMemoryUsageStore
from baton_herdr.core.model import TaskId
from baton_herdr.core.usage import (
    RateLimitObservation,
    RateLimitWindow,
    UsageRecord,
    UsageTokens,
)
from baton_herdr.ledger import open_event_store, open_usage_store

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from baton_herdr.core.ports import UsageStore
    from baton_herdr.core.usage import UsageEvent

AT = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)


def usage(record_id: str, at: datetime = AT, output: int = 10) -> UsageRecord:
    return UsageRecord(
        agent="claude",
        session_id="s",
        record_id=record_id,
        at=at,
        model="m",
        tokens=UsageTokens(input=1, output=output, cache_read=2, cache_write=3),
    )


def limits(at: datetime, used: float) -> RateLimitObservation:
    window = RateLimitWindow(
        name="primary", window_minutes=300, used_percent=used, resets_at=at + timedelta(hours=5)
    )
    return RateLimitObservation(agent="codex", session_id="c", at=at, windows=(window,))


@pytest.fixture(params=["sqlite", "memory"])
async def store(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[UsageStore]:
    """The SQLite store and the fake, held to the same behaviour."""
    if request.param == "memory":
        yield InMemoryUsageStore()
        return
    sqlite_store = await open_usage_store(tmp_path / "baton.db")
    yield sqlite_store
    await sqlite_store.close()


async def test_reading_a_log_again_stores_nothing_twice(store: UsageStore) -> None:
    first: list[UsageEvent] = [
        usage("claude:a"),
        usage("claude:b", AT + timedelta(minutes=1)),
        limits(AT, 1.0),
    ]
    assert await store.record(first) == 3
    # The same lines again (a restart reading from --since), plus one new.
    again: list[UsageEvent] = [
        *first,
        usage("claude:c", AT + timedelta(minutes=2)),
        limits(AT, 1.0),
    ]
    assert await store.record(again) == 1

    assert [r.record_id for r in await store.usage()] == ["claude:a", "claude:b", "claude:c"]
    assert [r.record_id for r in await store.usage(since=AT + timedelta(minutes=1))] == [
        "claude:b",
        "claude:c",
    ]
    assert await store.usage() == [*first[:2], again[3]]
    assert await store.rate_limits() == [limits(AT, 1.0)]
    assert await store.latest_at() == AT + timedelta(minutes=2)


async def test_an_empty_store_has_no_starting_point(store: UsageStore) -> None:
    assert await store.latest_at() is None


async def test_usage_tables_are_append_only_and_apart_from_the_events(tmp_path: Path) -> None:
    db = tmp_path / "baton.db"
    events = await open_event_store(db)
    await events.append(
        [
            TaskCreated(
                occurred_at=AT, task_id=TaskId("t"), title="t", instructions="-", workdir="/w"
            )
        ]
    )
    store = await open_usage_store(db)
    await store.record([usage("claude:a"), limits(AT, 1.0)])
    await store.close()

    with sqlite3.connect(db) as conn:
        for statement in (
            "UPDATE usage_records SET output = 0",
            "DELETE FROM usage_records",
            "UPDATE rate_limit_observations SET max_used_percent = 0",
            "DELETE FROM rate_limit_observations",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                conn.execute(statement)
    assert [s.event.task_id for s in await events.read()] == [TaskId("t")]
    await events.close()
