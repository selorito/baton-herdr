"""Bring a database file up to the current schema with Alembic."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from alembic import command
from alembic.config import Config

from baton_herdr.core.platform import exclusive_lock, running_on_wsl1

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def alembic_config(db_path: Path) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return config


def upgrade(db_path: Path) -> None:
    """Create the database if needed and apply every pending migration. Blocking.

    One process at a time: batond and a CLI command opening a fresh file together would
    both create the tables, and the second would fail. A lock file next to the database
    serialises them; the second then finds the schema current and does nothing.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = db_path.with_name(db_path.name + ".lock")
    with lock_path.open("a+", encoding="utf-8") as lock, exclusive_lock(lock):
        _upgrade(db_path)


def journal_mode() -> str:
    """WAL, except on WSL1: there WAL's shared memory does not work across processes, and
    every second process opening the file fails with SQLITE_PROTOCOL ("locking protocol"),
    so the CLI cannot reach the log batond holds. The rollback journal locks with fcntl,
    which WSL1 has (ADR 0022)."""
    return "DELETE" if running_on_wsl1() else "WAL"


def _upgrade(db_path: Path) -> None:
    command.upgrade(alembic_config(db_path), "head")
    # The journal mode is a property of the file. Set it here, once, before the stores
    # connect: switching takes an exclusive lock, which two engines opening a fresh file
    # at the same moment would fight over.
    with closing(sqlite3.connect(db_path, timeout=5)) as conn:
        conn.execute(f"PRAGMA journal_mode={journal_mode()}")
