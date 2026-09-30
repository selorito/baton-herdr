"""herdr's socket protocol: newline-delimited JSON over a Unix socket.

Shapes verified against herdr 0.9.1 (``fixtures/herdr/api-schema-0.9.1.json``):
a request is ``{"id", "method", "params"}``; a reply is ``{"id", "result"}`` or
``{"id", "error": {"code", "message"}}``. After ``events.subscribe`` the server
acknowledges with ``result.type == "subscription_started"`` and then pushes
``{"event", "data"}`` lines on the same connection.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from itertools import count
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

DEFAULT_TIMEOUT_S = 10.0
# pane.read replies can be large; asyncio's default line limit is 64 KiB.
LINE_LIMIT = 16 * 1024 * 1024


class HerdrApiError(Exception):
    """herdr answered with an error envelope."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class HerdrUnavailableError(Exception):
    """The socket cannot be reached, closed early, or timed out."""


class EventsLostError(Exception):
    """herdr dropped events for this subscription; cached state is stale."""


class HerdrSocket:
    """One short-lived connection per request; one long-lived one per subscription."""

    def __init__(self, socket_path: Path, *, timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        self._path = socket_path
        self._timeout_s = timeout_s
        self._ids = count(1)

    async def request(self, method: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Send one request and return its ``result`` object."""
        try:
            async with asyncio.timeout(self._timeout_s):
                reader, writer = await self._connect()
                try:
                    await self._send(writer, method, params or {})
                    reply = await _read_message(reader)
                finally:
                    await _close(writer)
        except TimeoutError as err:
            msg = f"herdr did not answer {method} within {self._timeout_s}s"
            raise HerdrUnavailableError(msg) from err
        return _unwrap(reply)

    async def subscribe(self, subscriptions: Sequence[Mapping[str, Any]]) -> Subscription:
        """Open a subscription and return it once herdr has acknowledged it.

        Returning only after the acknowledgement lets callers subscribe first and
        read current state second, so no change can fall between the two.
        """
        reader, writer = await self._connect()
        try:
            await self._send(writer, "events.subscribe", {"subscriptions": list(subscriptions)})
            async with asyncio.timeout(self._timeout_s):
                _unwrap(await _read_message(reader))
        except TimeoutError as err:
            await _close(writer)
            msg = f"herdr did not acknowledge the subscription within {self._timeout_s}s"
            raise HerdrUnavailableError(msg) from err
        except BaseException:
            await _close(writer)
            raise
        return Subscription(reader, writer)

    async def _connect(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        try:
            return await asyncio.open_unix_connection(str(self._path), limit=LINE_LIMIT)
        except OSError as err:
            msg = f"cannot connect to herdr at {self._path}: {err}"
            raise HerdrUnavailableError(msg) from err

    async def _send(
        self, writer: asyncio.StreamWriter, method: str, params: Mapping[str, Any]
    ) -> None:
        request = {"id": f"coban-{next(self._ids)}", "method": method, "params": dict(params)}
        writer.write(json.dumps(request).encode() + b"\n")
        await writer.drain()


class Subscription:
    """Pushed events as ``{"event", "data"}`` objects, until closed.

    Iteration raises :class:`EventsLostError` when herdr reports a gap and
    :class:`HerdrUnavailableError` when the connection ends.
    """

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer

    def __aiter__(self) -> Subscription:
        return self

    async def __anext__(self) -> dict[str, Any]:
        while True:
            message = await _read_message(self._reader)
            error = message.get("error")
            if isinstance(error, dict):
                if error.get("code") == "events_lost":
                    raise EventsLostError(str(error.get("message", "")))
                raise HerdrApiError(str(error.get("code")), str(error.get("message")))
            if "event" in message:
                return message

    async def aclose(self) -> None:
        await _close(self._writer)


async def _read_message(reader: asyncio.StreamReader) -> dict[str, Any]:
    try:
        line = await reader.readline()
    except (OSError, ValueError) as err:
        msg = f"reading from herdr failed: {err}"
        raise HerdrUnavailableError(msg) from err
    if not line:
        msg = "herdr closed the connection"
        raise HerdrUnavailableError(msg)
    try:
        message = json.loads(line)
    except json.JSONDecodeError as err:
        msg = "herdr sent a line that is not JSON"
        raise HerdrUnavailableError(msg) from err
    if not isinstance(message, dict):
        msg = "herdr sent JSON that is not an object"
        raise HerdrUnavailableError(msg)
    return message


def _unwrap(reply: dict[str, Any]) -> dict[str, Any]:
    error = reply.get("error")
    if isinstance(error, dict):
        raise HerdrApiError(str(error.get("code")), str(error.get("message")))
    result = reply.get("result")
    if not isinstance(result, dict):
        msg = "herdr reply has neither result nor error"
        raise HerdrUnavailableError(msg)
    return result


async def _close(writer: asyncio.StreamWriter) -> None:
    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()
