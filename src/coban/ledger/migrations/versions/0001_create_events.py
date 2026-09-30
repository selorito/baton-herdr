"""Create the append-only events table.

Revision ID: 0001
Revises:
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "events",
        sa.Column("seq", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("event_id", sa.Text(), nullable=False, unique=True),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.Text(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
    )
    op.create_index("ix_events_task_id", "events", ["task_id", "seq"])
    # The log is append-only (ADR 0002): the database itself refuses rewrites.
    op.execute(
        "CREATE TRIGGER events_no_update BEFORE UPDATE ON events "
        "BEGIN SELECT RAISE(ABORT, 'events are append-only'); END"
    )
    op.execute(
        "CREATE TRIGGER events_no_delete BEFORE DELETE ON events "
        "BEGIN SELECT RAISE(ABORT, 'events are append-only'); END"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER events_no_delete")
    op.execute("DROP TRIGGER events_no_update")
    op.drop_index("ix_events_task_id", table_name="events")
    op.drop_table("events")
