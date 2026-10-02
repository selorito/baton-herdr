"""Behaviour specific to the SQLite event store."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine

from baton_herdr.core.events import TaskCreated
from baton_herdr.core.model import TaskId
from baton_herdr.ledger import open_event_store
from baton_herdr.ledger.migrate import upgrade
from baton_herdr.ledger.schema import metadata

if TYPE_CHECKING:
    from pathlib import Path

NOW = datetime(2026, 9, 30, tzinfo=UTC)


def created(task: str) -> TaskCreated:
    return TaskCreated(
        occurred_at=NOW, task_id=TaskId(task), title=task, instructions="-", workdir="/w"
    )


async def test_events_survive_closing_and_reopening_the_database(tmp_path: Path) -> None:
    db = tmp_path / "baton.db"
    first = await open_event_store(db)
    appended = await first.append([created("t1"), created("t2")])
    await first.close()

    second = await open_event_store(db)  # also proves migrating twice is harmless
    try:
        assert await second.read() == list(appended)
        assert [s.seq for s in await second.append([created("t3")], expected_seq=2)] == [3]
    finally:
        await second.close()


async def test_the_database_refuses_to_rewrite_history(tmp_path: Path) -> None:
    db = tmp_path / "baton.db"
    store = await open_event_store(db)
    await store.append([created("t1")])
    await store.close()

    with sqlite3.connect(db) as raw:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            raw.execute("UPDATE events SET task_id = 'other'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            raw.execute("DELETE FROM events")
        assert raw.execute("SELECT count(*) FROM events").fetchone() == (1,)
    raw.close()


def test_migrations_produce_exactly_the_declared_schema(tmp_path: Path) -> None:
    db = tmp_path / "baton.db"
    upgrade(db)

    engine = create_engine(f"sqlite:///{db}")
    with engine.connect() as connection:
        differences = compare_metadata(MigrationContext.configure(connection), metadata)
    engine.dispose()

    assert differences == []
