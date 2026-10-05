"""CLI commands wake batond through a FIFO next to the event log."""

from __future__ import annotations

import asyncio
import stat
from typing import TYPE_CHECKING

from baton_herdr.daemon import listening, poke, wake_path

if TYPE_CHECKING:
    from pathlib import Path


def test_without_batond_a_poke_is_a_no_op(tmp_path: Path) -> None:
    db = tmp_path / "baton.db"
    assert poke(db) is False
    assert not wake_path(db).exists()  # the CLI creates nothing


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


async def test_a_path_that_is_not_a_fifo_is_left_alone(tmp_path: Path) -> None:
    db = tmp_path / "baton.db"
    wake_path(db).write_text("mine", encoding="utf-8")
    wake = asyncio.Event()
    with listening(wake_path(db), wake):
        pass
    assert wake_path(db).read_text(encoding="utf-8") == "mine"
    assert not wake.is_set()
