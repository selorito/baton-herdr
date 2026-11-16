"""CLI commands wake batond through a FIFO next to the event log; on Windows, which has
no FIFOs, through that file's modification time."""

from __future__ import annotations

import asyncio
import os
import stat
from typing import TYPE_CHECKING

from baton_herdr import daemon
from baton_herdr.daemon import listening, poke, wake_path

from platform_marks import posix_only

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_without_batond_a_poke_is_a_no_op(tmp_path: Path) -> None:
    db = tmp_path / "baton.db"
    assert poke(db) is False
    assert not wake_path(db).exists()  # the CLI creates nothing


@posix_only("FIFOs")
async def test_a_poke_wakes_the_listener_and_only_while_it_listens(tmp_path: Path) -> None:
    db = tmp_path / "data" / "baton.db"
    wake = asyncio.Event()
    with listening(wake_path(db), wake):
        fifo = wake_path(db)
        assert stat.S_ISFIFO(fifo.stat().st_mode)
        assert stat.S_IMODE(fifo.stat().st_mode) == 0o600
        assert poke(db) is True
        await asyncio.wait_for(wake.wait(), timeout=1)
        wake.clear()
        for _ in range(3):  # several pokes before the loop runs: one wake-up, no error
            poke(db)
        await asyncio.wait_for(wake.wait(), timeout=1)
    # The FIFO stays, but nobody reads it: a poke neither blocks nor succeeds.
    assert poke(db) is False


@posix_only("FIFOs")
async def test_a_path_that_is_not_a_fifo_is_left_alone(tmp_path: Path) -> None:
    db = tmp_path / "baton.db"
    wake_path(db).write_text("mine", encoding="utf-8")
    wake = asyncio.Event()
    with listening(wake_path(db), wake):
        pass
    assert wake_path(db).read_text(encoding="utf-8") == "mine"
    assert not wake.is_set()


async def test_windows_wakes_on_the_files_modification_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(daemon, "WAKE_POLL_S", 0.01)
    path = tmp_path / "data" / "baton.db.wake"
    wake = asyncio.Event()
    with daemon._watching_mtime(path, wake):
        assert path.exists()  # a poke finds it, so it knows a batond listens
        await asyncio.sleep(0.05)
        assert not wake.is_set()  # nothing changed yet
        stat_before = path.stat().st_mtime_ns
        os.utime(path, ns=(stat_before + 1_000_000_000, stat_before + 1_000_000_000))
        await asyncio.wait_for(wake.wait(), timeout=1)
