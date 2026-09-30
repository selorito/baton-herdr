"""SQLite implementation of :class:`coban.core.ports.EventStore`."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from sqlalchemy import event, func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from coban.core.events import EVENT_ADAPTER, Event, StoredEvent
from coban.core.ports import ConcurrencyError, DuplicateEventError
from coban.ledger.migrate import upgrade
from coban.ledger.schema import events

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from sqlalchemy.engine import Connection

    from coban.core.model import TaskId

BUSY_TIMEOUT_MS = 5000


def create_engine(db_path: Path) -> AsyncEngine:
    """Engine whose write transactions take SQLite's write lock up front.

    ``BEGIN IMMEDIATE`` makes concurrent appends queue instead of failing
    halfway with SQLITE_BUSY, so "read last seq, then insert" is atomic.
    """
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")

    @event.listens_for(engine.sync_engine, "connect")
    def _on_connect(dbapi_connection: Any, _record: object) -> None:
        # Take transaction control away from the driver; see do_begin below.
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        cursor.close()

    @event.listens_for(engine.sync_engine, "begin")
    def _on_begin(connection: Connection) -> None:
        connection.exec_driver_sql("BEGIN IMMEDIATE")

    return engine


async def open_event_store(db_path: Path) -> SqliteEventStore:
    """Migrate ``db_path`` to the current schema and open it."""
    await asyncio.to_thread(upgrade, db_path)
    return SqliteEventStore(create_engine(db_path))


class SqliteEventStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        # Reads do not need the write lock that BEGIN IMMEDIATE takes.
        self._reads = engine.execution_options(isolation_level="AUTOCOMMIT")

    async def close(self) -> None:
        await self._engine.dispose()

    async def append(
        self, new_events: Sequence[Event], *, expected_seq: int | None = None
    ) -> Sequence[StoredEvent]:
        async with self._engine.begin() as conn:
            current: int = (
                await conn.execute(select(func.coalesce(func.max(events.c.seq), 0)))
            ).scalar_one()
            if expected_seq is not None and expected_seq != current:
                raise ConcurrencyError(expected_seq=expected_seq, actual_seq=current)
            stored = [
                StoredEvent(seq=current + offset, event=item)
                for offset, item in enumerate(new_events, start=1)
            ]
            if stored:
                try:
                    await conn.execute(insert(events), [_to_row(s) for s in stored])
                except IntegrityError as err:
                    raise DuplicateEventError from err
        return stored

    async def read(
        self, *, after_seq: int = 0, task_id: TaskId | None = None
    ) -> Sequence[StoredEvent]:
        query = select(events.c.seq, events.c.payload).where(events.c.seq > after_seq)
        if task_id is not None:
            query = query.where(events.c.task_id == task_id)
        async with self._reads.connect() as conn:
            rows = (await conn.execute(query.order_by(events.c.seq))).all()
        return [
            StoredEvent(seq=seq, event=EVENT_ADAPTER.validate_json(payload))
            for seq, payload in rows
        ]

    async def last_seq(self) -> int:
        async with self._reads.connect() as conn:
            result = await conn.execute(select(func.coalesce(func.max(events.c.seq), 0)))
            last: int = result.scalar_one()
        return last


def _to_row(stored: StoredEvent) -> dict[str, object]:
    item = stored.event
    return {
        "seq": stored.seq,
        "event_id": str(item.event_id),
        "type": item.type,
        "task_id": item.task_id,
        "occurred_at": item.occurred_at.isoformat(),
        "payload": EVENT_ADAPTER.dump_json(item).decode(),
    }
