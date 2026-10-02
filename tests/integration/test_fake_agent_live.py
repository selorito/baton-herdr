"""fake-agent in a real herdr pane, read back through baton. Run with ``just smoke``.

Proves the pieces the end-to-end demo relies on: herdr recognises the fake as a
Claude process and classifies its screens with its own Claude rules, the
session id reported like Claude's herdr hook is visible, ``agent prompt``
reaches it, and baton's observation plus the Claude adapter see the limit.
"""

from __future__ import annotations

import asyncio
import shlex
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from baton_herdr.adapters import ADAPTERS
from baton_herdr.core.detection import DetectionRequest
from baton_herdr.core.model import AgentKind, AgentState

import fake_agent

if TYPE_CHECKING:
    from baton_herdr.herdr.host import HerdrPaneHost

pytestmark = pytest.mark.live

FAKE = Path(__file__).parents[2] / "tools" / "fake-agent"


async def until(predicate_text: str, read: object, timeout_s: float = 10) -> None:
    assert callable(read)
    async with asyncio.timeout(timeout_s):
        while predicate_text not in await read():
            await asyncio.sleep(0.1)


async def test_fake_claude_hits_its_limit_and_baton_sees_it(
    isolated_host: HerdrPaneHost, tmp_path: Path
) -> None:
    host = isolated_host
    pane_id = await host.open_pane(cwd=str(tmp_path), label="fake claude")
    # Run under the name "claude" so herdr recognises the process, as for the real agent.
    launcher = fake_agent.make_launcher(tmp_path / "bin", "claude")
    command = shlex.join(
        [
            str(launcher),
            str(FAKE / "scripts" / "claude-limit.toml"),
            "--session-id",
            "fake-session-1",
        ]
    )
    await host.send_text(pane_id, command)
    await host.send_keys(pane_id, ["Enter"])

    async with asyncio.timeout(10):
        while (observation := await host.observe(pane_id)).agent is not AgentKind.CLAUDE:
            await asyncio.sleep(0.1)
    assert observation.session_ref == "fake-session-1"

    await host.send_prompt(pane_id, "add a power function")
    await until("You've hit your session limit", lambda: host.read_screen(pane_id, lines=40))

    observation = await host.observe(pane_id)
    result = ADAPTERS[AgentKind.CLAUDE].classify(
        DetectionRequest(
            agent=AgentKind.CLAUDE,
            screen=await host.read_screen(pane_id, lines=40),
            host_state=observation.state,
            host_evidence=observation.evidence,
            observed_at=datetime.now(UTC),
        )
    )
    assert result.state is AgentState.RATE_LIMITED
    assert result.resets_at is not None
