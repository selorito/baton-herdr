"""Table definitions. Changes here need a migration in ``migrations/versions``."""

from __future__ import annotations

from sqlalchemy import Column, Index, Integer, MetaData, Table, Text

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
