"""Smoke test against a real herdr server. Opt in with COBAN_LIVE_HERDR=1.

It opens a background pane running a shell, drives it, and closes it again. No
agent is started, so it spends no quota.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import aclosing
from typing import TYPE_CHECKING

import pytest

from coban.core.config import HerdrSettings
from coban.core.model import AgentState
from coban.core.panes import PaneNotFoundError
from coban.herdr import connect

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.skipif(
    os.environ.get("COBAN_LIVE_HERDR") != "1", reason="set COBAN_LIVE_HERDR=1 to run"
)


async def test_drive_a_shell_pane_through_a_real_herdr(tmp_path: Path) -> None:
    host = connect(HerdrSettings())
    pane_id = await host.open_pane(cwd=str(tmp_path), label="coban live test")
    try:
        observation = await host.observe(pane_id)
        assert (observation.agent, observation.state) == (None, AgentState.UNKNOWN)
        assert observation.cwd == str(tmp_path)

        await host.send_text(pane_id, "echo coban-$((20 + 22))")
        await host.send_keys(pane_id, ["Enter"])
        for _ in range(50):
            if "coban-42" in await host.read_screen(pane_id, lines=20):
                break
            await asyncio.sleep(0.1)
        else:
            pytest.fail("the command output never appeared")

        assert await host.processes(pane_id) is not None
        async with aclosing(host.watch(pane_id)) as changes:
            first = await asyncio.wait_for(anext(changes), 5)
        assert first.state is AgentState.UNKNOWN
    finally:
        await host.close_pane(pane_id)

    with pytest.raises(PaneNotFoundError):
        await host.observe(pane_id)
