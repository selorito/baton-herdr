"""SQLite implementation of :class:`baton_herdr.core.ports.UsageStore` (ADR 0011).

Usage telemetry lives in its own append-only tables next to the event log, in the
same database file. Re-reading a log yields the same records again; the database
drops them (``INSERT … ON CONFLICT DO NOTHING``), so ``--since`` only has to be a
rough starting point.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select, union_all
from sqlalchemy.dialects.sqlite import insert

from baton_herdr.core.usage import RateLimitObservation, RateLimitWindow, UsageRecord, UsageTokens
from baton_herdr.ledger.migrate import upgrade
from baton_herdr.ledger.schema import rate_limit_observations, usage_records
from baton_herdr.ledger.store import create_engine

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncEngine

    from baton_herdr.core.usage import UsageEvent


async def open_usage_store(db_path: Path) -> SqliteUsageStore:
    """Migrate ``db_path`` to the current schema and open its usage tables."""
    await asyncio.to_thread(upgrade, db_path)
    return SqliteUsageStore(create_engine(db_path))


def _text(at: datetime) -> str:
    return at.astimezone(UTC).isoformat(timespec="milliseconds")


class SqliteUsageStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._reads = engine.execution_options(isolation_level="AUTOCOMMIT")

    async def close(self) -> None:
        await self._engine.dispose()

    async def record(self, events: Sequence[UsageEvent]) -> int:
        new = 0
        async with self._engine.begin() as conn:
            for item in events:
                if isinstance(item, UsageRecord):
                    statement = insert(usage_records).values(_usage_row(item))
                    statement = statement.on_conflict_do_nothing(index_elements=["record_id"])
                else:
                    statement = insert(rate_limit_observations).values(_limits_row(item))
                    statement = statement.on_conflict_do_nothing(
                        index_elements=["agent", "session_id", "at"]
                    )
                result = await conn.execute(statement)
                new += result.rowcount or 0
        return new

    async def latest_at(self) -> datetime | None:
        times = union_all(
            select(func.max(usage_records.c.at).label("at")),
            select(func.max(rate_limit_observations.c.at).label("at")),
        ).subquery()
        async with self._reads.connect() as conn:
            latest: str | None = (await conn.execute(select(func.max(times.c.at)))).scalar()
        return datetime.fromisoformat(latest) if latest else None

    async def usage(self, *, since: datetime | None = None) -> Sequence[UsageRecord]:
        query = select(usage_records).order_by(usage_records.c.at, usage_records.c.record_id)
        if since is not None:
            query = query.where(usage_records.c.at >= _text(since))
        async with self._reads.connect() as conn:
            rows = (await conn.execute(query)).mappings().all()
        return [_usage(row) for row in rows]

    async def rate_limits(self, *, since: datetime | None = None) -> Sequence[RateLimitObservation]:
        table = rate_limit_observations
        query = select(table).order_by(table.c.at, table.c.id)
        if since is not None:
            query = query.where(table.c.at >= _text(since))
        async with self._reads.connect() as conn:
            rows = (await conn.execute(query)).mappings().all()
        return [
            RateLimitObservation(
                agent=row["agent"],
                session_id=row["session_id"],
                at=datetime.fromisoformat(row["at"]),
                windows=tuple(
                    RateLimitWindow.model_validate(w) for w in json.loads(row["windows"])
                ),
            )
            for row in rows
        ]


def _usage_row(item: UsageRecord) -> dict[str, Any]:
    tokens = item.tokens
    return {
        "record_id": item.record_id,
        "agent": item.agent,
        "session_id": item.session_id,
        "at": _text(item.at),
        "model": item.model,
        "input": tokens.input,
        "output": tokens.output,
        "cache_read": tokens.cache_read,
        "cache_write": tokens.cache_write,
        "reasoning": tokens.reasoning,
    }


def _usage(row: Any) -> UsageRecord:
    return UsageRecord(
        agent=row["agent"],
        session_id=row["session_id"],
        record_id=row["record_id"],
        at=datetime.fromisoformat(row["at"]),
        model=row["model"],
        tokens=UsageTokens(
            input=row["input"],
            output=row["output"],
            cache_read=row["cache_read"],
            cache_write=row["cache_write"],
            reasoning=row["reasoning"],
        ),
    )


def _limits_row(item: RateLimitObservation) -> dict[str, Any]:
    return {
        "agent": item.agent,
        "session_id": item.session_id,
        "at": _text(item.at),
        "windows": json.dumps([w.model_dump(mode="json") for w in item.windows]),
        "max_used_percent": max(w.used_percent for w in item.windows),
    }
