"""baton reaches herdr on Windows over the named pipe herdr serves for its socket path.

Verified against herdr 0.9.5 on Windows 11: ``herdr.sock`` is a marker file and the API is
the pipe ``\\\\.\\pipe\\<socket path>``. Here a pipe server stands in for herdr.
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import TYPE_CHECKING, Any

import pytest

from baton_herdr.core.platform import pipe_name
from baton_herdr.herdr.transport import HerdrSocket

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows named pipes")


class FakeHerdr(asyncio.Protocol):
    """Answers each request line with its method; ``events.subscribe`` then pushes one."""

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport: Any = transport
        self.buffer = b""

    def data_received(self, data: bytes) -> None:
        self.buffer += data
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            request = json.loads(line)
            if request["method"] == "events.subscribe":
                reply = {"id": request["id"], "result": {"type": "subscription_started"}}
                event = {"event": "pane.agent_status_changed", "data": {"pane_id": "w1:p1"}}
                self.transport.write(json.dumps(reply).encode() + b"\n")
                self.transport.write(json.dumps(event).encode() + b"\n")
            else:
                result = {"type": "echo", "method": request["method"], "ğüş": "İ"}
                self.transport.write(
                    json.dumps({"id": request["id"], "result": result}).encode() + b"\n"
                )


async def test_requests_and_subscriptions_go_over_the_named_pipe(tmp_path: Path) -> None:
    socket_path = tmp_path / "herdr.sock"
    loop = asyncio.get_running_loop()
    servers = await loop.start_serving_pipe(FakeHerdr, pipe_name(socket_path))  # type: ignore[attr-defined]
    try:
        herdr = HerdrSocket(socket_path, timeout_s=5)
        assert await herdr.request("workspace.list") == {
            "type": "echo",
            "method": "workspace.list",
            "ğüş": "İ",
        }
        # Several at once: every pipe instance busy is waited out, not an error.
        replies = await asyncio.gather(*(herdr.request("pane.get") for _ in range(8)))
        assert all(r["method"] == "pane.get" for r in replies)
        subscription = await herdr.subscribe([{"type": "pane.agent_status_changed"}])
        event = await asyncio.wait_for(anext(subscription), 5)
        assert event["data"] == {"pane_id": "w1:p1"}
        await subscription.aclose()
    finally:
        for server in servers:
            server.close()
