"""Smoke test against a real herdr binary, in a throwaway named session.

Run with ``just smoke``. The test starts its own headless herdr server under a
unique session name (its own socket and state directory), so it never touches
the panes of a herdr the developer has open. The fixture stops the server and
deletes the session even when the test fails. No agent is started, so it spends
no quota. Excluded from the default run and from CI by the ``live`` marker.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from contextlib import aclosing
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from coban.core.config import HerdrSettings, resolve_herdr_socket_path
from coban.core.model import AgentState
from coban.core.panes import PaneHostUnavailableError, PaneNotFoundError
from coban.herdr.host import HerdrPaneHost
from coban.herdr.transport import HerdrSocket

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

pytestmark = pytest.mark.live

# herdr sets these inside its panes; they would point the CLI at the user's server.
_INHERITED = ("HERDR_SOCKET_PATH", "HERDR_CLIENT_SOCKET_PATH", "HERDR_SESSION")


async def _herdr(binary: str, session: str, *args: str, env: dict[str, str]) -> int:
    process = await asyncio.create_subprocess_exec(
        binary,
        "--session",
        session,
        *args,
        env=env,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    return await process.wait()


@pytest.fixture
async def isolated_host() -> AsyncIterator[HerdrPaneHost]:
    binary = shutil.which("herdr")
    if binary is None:
        pytest.skip("herdr is not installed")
    session = f"coban-smoke-{os.getpid()}"
    env = {key: value for key, value in os.environ.items() if key not in _INHERITED}
    socket_path = resolve_herdr_socket_path(
        HerdrSettings(session=session), env=env, home=Path.home()
    )
    server = await asyncio.create_subprocess_exec(
        binary,
        "--session",
        session,
        "server",
        env=env,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    host = HerdrPaneHost(HerdrSocket(socket_path, timeout_s=5))
    try:
        for _ in range(100):
            try:
                await host.open_pane(cwd=str(Path.home()), label="probe")
                break
            except PaneHostUnavailableError:
                await asyncio.sleep(0.1)
        else:
            pytest.fail("the isolated herdr server did not start")
        yield host
    finally:
        # Stopping the server ends every pane in the session, whatever the test left open.
        await _herdr(binary, session, "server", "stop", env=env)
        try:
            await asyncio.wait_for(server.wait(), 15)
        except TimeoutError:
            server.kill()
            await server.wait()
        await _herdr(binary, session, "session", "delete", session, env=env)


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
