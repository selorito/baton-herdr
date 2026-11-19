"""baton's process fallback against herdr's own detection, in a real herdr (ADR 0022).

On Linux herdr sees the pane's processes, so the same pane can be read both ways: through
herdr's detection and through the fallback that baton uses where herdr cannot (WSL1). Every
moment the two can be compared they must agree. On WSL1 herdr sees no agent at all; there
the test checks the fallback alone.
"""

from __future__ import annotations

import asyncio
import shlex
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from baton_herdr.core.model import AgentKind, AgentState
from baton_herdr.core.platform import running_on_wsl1
from baton_herdr.herdr import process_fallback
from baton_herdr.herdr.host import HerdrPaneHost

import fake_agent

if TYPE_CHECKING:
    from baton_herdr.core.panes import PaneObservation

pytestmark = pytest.mark.live

FAKE = Path(__file__).parents[2] / "tools" / "fake-agent"
SCRIPTS = {
    "claude-finish": AgentKind.CLAUDE,
    "claude-limit": AgentKind.CLAUDE,
    "claude-permission": AgentKind.CLAUDE,
    "claude-question": AgentKind.CLAUDE,
    "codex-finish": AgentKind.CODEX,
}
RUN_S = 14.0


@pytest.fixture
def fallback_host(isolated_host: HerdrPaneHost) -> HerdrPaneHost:
    """A second host on the same herdr server, reading panes the fallback's way."""
    return HerdrPaneHost(isolated_host._socket, fallback=process_fallback())


@pytest.mark.parametrize("script", sorted(SCRIPTS))
async def test_the_fallback_reads_a_pane_as_herdr_does(
    isolated_host: HerdrPaneHost, fallback_host: HerdrPaneHost, tmp_path: Path, script: str
) -> None:
    agent = SCRIPTS[script]
    pane_id = await isolated_host.open_pane(cwd=str(tmp_path), label=script)
    launcher = fake_agent.make_launcher(tmp_path / "bin", agent.value)
    command = shlex.join(
        [str(launcher), str(FAKE / "scripts" / f"{script}.toml"), "--session-id", "fake-1"]
    )
    await isolated_host.send_text(pane_id, command)
    await isolated_host.send_keys(pane_id, ["Enter"])

    herdr_blind = running_on_wsl1()
    compared = 0
    mismatches: list[tuple[PaneObservation, PaneObservation]] = []
    seen: set[AgentState] = set()
    sessions: set[str | None] = set()
    prompts = 0
    loop = asyncio.get_running_loop()
    deadline = loop.time() + RUN_S
    while loop.time() < deadline:
        before = await isolated_host.observe(pane_id)
        ours = await fallback_host.observe(pane_id)
        after = await isolated_host.observe(pane_id)
        if ours.agent is not None:
            seen.add(ours.state)
            assert ours.agent is agent
            # The agent reports its session a moment after it starts.
            sessions.add(ours.session_ref)
        stable = (before.agent, before.state) == (after.agent, after.state)
        if not herdr_blind and stable and before.agent is not None:
            compared += 1
            if (ours.agent, ours.state) != (before.agent, before.state):
                mismatches.append((before, ours))
        if ours.state is AgentState.IDLE and prompts < 2:
            # The fake waits for a line on its prompt box; send it the fallback's way.
            await fallback_host.send_prompt(pane_id, f"turn {prompts + 1}")
            prompts += 1
        await asyncio.sleep(0.25)

    assert seen, "the fallback never saw the agent"
    assert "fake-1" in sessions
    assert prompts >= 1, f"the fallback never saw the agent idle: {seen}"
    if not herdr_blind:
        assert compared > 0
        assert mismatches == [], [
            (h.state.value, h.evidence, o.state.value, o.evidence) for h, o in mismatches
        ]


async def test_processes_follow_the_agent_in_and_out(
    fallback_host: HerdrPaneHost, tmp_path: Path
) -> None:
    pane_id = await fallback_host.open_pane(cwd=str(tmp_path), label="processes")
    launcher = fake_agent.make_launcher(tmp_path / "bin", "claude")
    script = FAKE / "scripts" / "claude-finish.toml"
    await fallback_host.send_text(pane_id, f"{shlex.quote(str(launcher))} {script}")
    await fallback_host.send_keys(pane_id, ["Enter"])

    async with asyncio.timeout(10):
        while (await fallback_host.observe(pane_id)).agent is not AgentKind.CLAUDE:
            await asyncio.sleep(0.1)
    commands = [p.command for p in await fallback_host.processes(pane_id)]
    assert any(str(launcher) in command for command in commands), commands

    await fallback_host.send_keys(pane_id, ["ctrl+c"])
    async with asyncio.timeout(10):
        while (await fallback_host.observe(pane_id)).agent is not None:
            await asyncio.sleep(0.1)
    shells = [p.command for p in await fallback_host.processes(pane_id)]
    assert len(shells) == 1
    assert Path(shells[0].split()[0].lstrip("-")).name in {"bash", "sh", "zsh", "fish", "dash"}
