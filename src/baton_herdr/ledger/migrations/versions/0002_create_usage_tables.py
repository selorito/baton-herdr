"""Create the usage telemetry tables (ADR 0011).

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

_APPEND_ONLY = ("usage_records", "rate_limit_observations")


def upgrade() -> None:
    op.create_table(
        "usage_records",
        sa.Column("record_id", sa.Text(), primary_key=True),
        sa.Column("agent", sa.Text(), nullable=False),
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column("at", sa.Text(), nullable=False),
        sa.Column("model", sa.Text()),
        sa.Column("input", sa.Integer(), nullable=False),
        sa.Column("output", sa.Integer(), nullable=False),
        sa.Column("cache_read", sa.Integer(), nullable=False),
        sa.Column("cache_write", sa.Integer(), nullable=False),
        sa.Column("reasoning", sa.Integer()),
    )
    op.create_index("ix_usage_records_agent_at", "usage_records", ["agent", "at"])
    op.create_table(
        "rate_limit_observations",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("agent", sa.Text(), nullable=False),
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column("at", sa.Text(), nullable=False),
        sa.Column("windows", sa.Text(), nullable=False),
        sa.Column("max_used_percent", sa.Float(), nullable=False),
        sa.UniqueConstraint("agent", "session_id", "at", name="uq_rate_limit_observations_key"),
    )
    op.create_index(
        "ix_rate_limit_observations_agent_at", "rate_limit_observations", ["agent", "at"]
    )
    for table in _APPEND_ONLY:
        for action in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER {table}_no_{action.lower()} BEFORE {action} ON {table} "
                f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END"
            )


def downgrade() -> None:
    for table in _APPEND_ONLY:
        for action in ("update", "delete"):
            op.execute(f"DROP TRIGGER {table}_no_{action}")
    op.drop_index("ix_rate_limit_observations_agent_at", table_name="rate_limit_observations")
    op.drop_table("rate_limit_observations")
    op.drop_index("ix_usage_records_agent_at", table_name="usage_records")
    op.drop_table("usage_records")
