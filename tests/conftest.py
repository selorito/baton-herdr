"""Fixtures shared by all tests."""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from coban.core.config import HerdrSettings, resolve_herdr_socket_path
from coban.core.panes import PaneHostUnavailableError
from coban.herdr.host import HerdrPaneHost
from coban.herdr.transport import HerdrSocket

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep the developer's own COBAN_* variables and .env out of every test."""
    for key in list(os.environ):
        if key.startswith("COBAN_"):
            monkeypatch.delenv(key)
    monkeypatch.chdir(tmp_path)


# herdr sets these inside its panes; they would point the CLI at the user's server.
_INHERITED = ("HERDR_SOCKET_PATH", "HERDR_CLIENT_SOCKET_PATH", "HERDR_SESSION")
# When the tests run inside a coding agent, its session variables would leak into the
# throwaway server's panes and change how agents started there behave (seen live: a
# Claude Code started with them wrote no transcript and could not be resumed).
_AGENT_SESSION_PREFIXES = ("CLAUDE", "CODEX_")


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
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in _INHERITED and not key.startswith(_AGENT_SESSION_PREFIXES)
    }
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
