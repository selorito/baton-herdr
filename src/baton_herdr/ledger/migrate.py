"""Bring a database file up to the current schema with Alembic."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from alembic import command
from alembic.config import Config

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def alembic_config(db_path: Path) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return config


def upgrade(db_path: Path) -> None:
    """Create the database if needed and apply every pending migration. Blocking."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(db_path), "head")
    # WAL is a property of the file. Set it here, once, before the stores connect:
    # switching takes an exclusive lock, which two engines opening a fresh file at
    # the same moment would fight over.
    with closing(sqlite3.connect(db_path, timeout=5)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
