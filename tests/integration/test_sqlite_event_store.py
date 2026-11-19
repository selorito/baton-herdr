"""Behaviour specific to the SQLite event store."""

from __future__ import annotations

import asyncio
import sqlite3
import subprocess
import sys
from contextlib import closing
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


def test_processes_opening_a_new_database_together_all_get_the_schema(tmp_path: Path) -> None:
    """batond and the CLI may create the database at the same moment (seen in `just demo`)."""
    db = tmp_path / "new" / "baton.db"
    code = (
        "import sys; from pathlib import Path; "
        "from baton_herdr.ledger.migrate import upgrade; upgrade(Path(sys.argv[1]))"
    )
    runs = [
        subprocess.Popen(  # noqa: S603 - this interpreter, a fixed script
            [sys.executable, "-c", code, str(db)], stderr=subprocess.PIPE, text=True
        )
        for _ in range(6)
    ]
    errors = [run.communicate(timeout=60)[1] for run in runs]
    failures = [e[-300:] for run, e in zip(runs, errors, strict=True) if run.returncode != 0]
    assert not failures, failures


@pytest.mark.parametrize(("on_wsl1", "expected"), [(False, "wal"), (True, "delete")])
def test_the_journal_suits_the_system(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, on_wsl1: bool, expected: str
) -> None:
    """WSL1's shared memory does not hold WAL together across processes (ADR 0022)."""
    monkeypatch.setattr("baton_herdr.ledger.migrate.running_on_wsl1", lambda: on_wsl1)
    db = tmp_path / "baton.db"

    upgrade(db)

    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone() == (expected,)


def test_a_wal_database_moved_to_wsl1_leaves_wal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "baton.db"
    monkeypatch.setattr("baton_herdr.ledger.migrate.running_on_wsl1", lambda: False)
    upgrade(db)
    monkeypatch.setattr("baton_herdr.ledger.migrate.running_on_wsl1", lambda: True)

    upgrade(db)

    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone() == ("delete",)


async def test_another_process_reads_the_log_batond_holds_open(tmp_path: Path) -> None:
    """The CLI reads and appends while batond keeps its store open. With WAL on WSL1
    the second process failed with "locking protocol"."""
    db = tmp_path / "baton.db"
    held = await open_event_store(db)
    await held.append([created("t1")])
    code = (
        "import asyncio, sys; from pathlib import Path; "
        "from baton_herdr.ledger import open_event_store\n"
        "async def main():\n"
        "    store = await open_event_store(Path(sys.argv[1]))\n"
        "    print(len(await store.read()))\n"
        "    await store.close()\n"
        "asyncio.run(main())"
    )
    try:
        run = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            code,
            str(db),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(run.communicate(), 60)
    finally:
        await held.close()

    assert run.returncode == 0, err.decode()[-500:]
    assert out.decode().strip() == "1"
