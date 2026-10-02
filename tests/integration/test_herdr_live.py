"""Smoke test against a real herdr binary, in a throwaway named session.

Run with ``just smoke``. The test starts its own headless herdr server under a
unique session name (its own socket and state directory), so it never touches
the panes of a herdr the developer has open. The fixture stops the server and
deletes the session even when the test fails. No agent is started, so it spends
no quota. Excluded from the default run and from CI by the ``live`` marker.
"""

from __future__ import annotations

import asyncio
from contextlib import aclosing
from typing import TYPE_CHECKING

import pytest

from coban.core.model import AgentState
from coban.core.panes import PaneNotFoundError

if TYPE_CHECKING:
    from pathlib import Path

    from coban.herdr.host import HerdrPaneHost

pytestmark = pytest.mark.live


async def test_drive_a_shell_pane_through_a_real_herdr(
    isolated_host: HerdrPaneHost, tmp_path: Path
) -> None:
    host = isolated_host
    pane_id = await host.open_pane(cwd=str(tmp_path), label="coban live test")

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

    await host.close_pane(pane_id)
    with pytest.raises(PaneNotFoundError):
        await host.observe(pane_id)
