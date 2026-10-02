"""Table definitions. Changes here need a migration in ``migrations/versions``."""

from __future__ import annotations

from sqlalchemy import Column, Float, Index, Integer, MetaData, Table, Text, UniqueConstraint

metadata = MetaData()

events = Table(
    "events",
    metadata,
    # Assigned by the store: contiguous, starting at 1. Not AUTOINCREMENT.
    Column("seq", Integer, primary_key=True, autoincrement=False),
    Column("event_id", Text, nullable=False, unique=True),
    Column("type", Text, nullable=False),
    Column("task_id", Text, nullable=False),
    Column("occurred_at", Text, nullable=False),
    # The full event as JSON; the other columns are copies for filtering.
    Column("payload", Text, nullable=False),
    Index("ix_events_task_id", "task_id", "seq"),
)

# Agent usage telemetry (ADR 0011): not task events, never folded into the board.
# Times are UTC ISO 8601 with milliseconds, so text order is time order.
usage_records = Table(
    "usage_records",
    metadata,
    Column("record_id", Text, primary_key=True),
    Column("agent", Text, nullable=False),
    Column("session_id", Text, nullable=False),
    Column("at", Text, nullable=False),
    Column("model", Text),
    Column("input", Integer, nullable=False),
    Column("output", Integer, nullable=False),
    Column("cache_read", Integer, nullable=False),
    Column("cache_write", Integer, nullable=False),
    Column("reasoning", Integer),
    Index("ix_usage_records_agent_at", "agent", "at"),
)

rate_limit_observations = Table(
    "rate_limit_observations",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("agent", Text, nullable=False),
    Column("session_id", Text, nullable=False),
    Column("at", Text, nullable=False),
    # The windows as JSON: [{"name", "window_minutes", "used_percent", "resets_at"}].
    Column("windows", Text, nullable=False),
    # The highest used_percent of any window, for quick filtering.
    Column("max_used_percent", Float, nullable=False),
    UniqueConstraint("agent", "session_id", "at", name="uq_rate_limit_observations_key"),
    Index("ix_rate_limit_observations_agent_at", "agent", "at"),
)
