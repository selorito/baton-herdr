"""``baton-detect classify`` as a :class:`~baton_herdr.core.detector.Detector`.

One long-running process answers one request per line, in order. Anything that goes
wrong with it (missing binary, a crash, a late or unreadable answer) is a
:class:`DetectorUnavailableError`; the process is then stopped and started again on the
next request.

``FallbackDetector`` puts it in front of a fallback (batond's is herdr's own state,
``HostDetector``): an outage is logged once, and while the process keeps failing it is
not tried again before ``retry_s``.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import TYPE_CHECKING

from pydantic import ValidationError

from baton_herdr.core.detection import DetectionResult
from baton_herdr.core.executables import detector_binary
from baton_herdr.core.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

    from baton_herdr.core.detection import DetectionRequest
    from baton_herdr.core.detector import Detector

# Enough for the error line the binary writes before it exits.
_STDERR_TAIL = 2000


class DetectorUnavailableError(Exception):
    """The Rust detector gave no answer; the message says why."""


class ProcessDetector:
    def __init__(self, binary: str, *, timeout_s: float = 5) -> None:
        self._binary = binary
        self._timeout_s = timeout_s
        self._process: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()

    async def classify(self, request: DetectionRequest) -> DetectionResult:
        async with self._lock:
            try:
                return await self._ask(request)
            except DetectorUnavailableError:
                await self._stop()
                raise

    async def _ask(self, request: DetectionRequest) -> DetectionResult:
        process = await self._start()
        if process.stdin is None or process.stdout is None:  # pragma: no cover - PIPE
            raise DetectorUnavailableError("no pipes to baton-detect")
        try:
            process.stdin.write(request.model_dump_json().encode() + b"\n")
            await process.stdin.drain()
            line = await asyncio.wait_for(process.stdout.readline(), self._timeout_s)
        except TimeoutError as err:
            raise DetectorUnavailableError(f"no answer within {self._timeout_s:g} s") from err
        except (BrokenPipeError, ConnectionResetError) as err:
            raise DetectorUnavailableError(await self._exit_reason(process)) from err
        if not line:
            raise DetectorUnavailableError(await self._exit_reason(process))
        try:
            return DetectionResult.model_validate_json(line)
        except ValidationError as err:
            raise DetectorUnavailableError(f"unreadable answer: {err}") from err

    async def _start(self) -> asyncio.subprocess.Process:
        if self._process is not None and self._process.returncode is None:
            return self._process
        binary = detector_binary(self._binary)
        if binary is None:
            raise DetectorUnavailableError(f"{self._binary} not found")
        try:
            self._process = await asyncio.create_subprocess_exec(
                str(binary),
                "classify",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as err:
            raise DetectorUnavailableError(f"{binary} does not start: {err}") from err
        return self._process

    async def _exit_reason(self, process: asyncio.subprocess.Process) -> str:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(process.wait(), 1)
        stderr = b""
        if process.stderr is not None:
            with contextlib.suppress(TimeoutError):
                stderr = await asyncio.wait_for(process.stderr.read(_STDERR_TAIL), 1)
        message = stderr.decode(errors="replace").strip()
        return f"baton-detect exited ({process.returncode}): {message or 'no message'}"

    async def _stop(self) -> None:
        process, self._process = self._process, None
        if process is None or process.returncode is not None:
            return
        if process.stdin is not None:
            process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), 2)
        except TimeoutError:
            process.kill()
            await process.wait()

    async def aclose(self) -> None:
        async with self._lock:
            await self._stop()


class _Gate:
    """Skip an unavailable detector for ``retry_s``; log the first failure of an outage."""

    def __init__(self, name: str, retry_s: float, now: Callable[[], float]) -> None:
        self._name = name
        self._retry_s = retry_s
        self._now = now
        self._down_since: float | None = None
        self._log = get_logger("baton.detector")

    def open(self) -> bool:
        return self._down_since is None or self._now() - self._down_since >= self._retry_s

    def failed(self, err: DetectorUnavailableError) -> None:
        if self._down_since is None:
            self._log.warning("detector unavailable", mode=self._name, error=str(err))
        self._down_since = self._now()

    def succeeded(self) -> None:
        if self._down_since is not None:
            self._log.info("detector available again", mode=self._name)
        self._down_since = None


class FallbackDetector:
    """``primary`` decides while it is available; ``fallback`` answers otherwise."""

    def __init__(
        self,
        primary: Detector,
        fallback: Detector,
        *,
        retry_s: float = 60,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._gate = _Gate("rust", retry_s, now)

    async def classify(self, request: DetectionRequest) -> DetectionResult:
        if self._gate.open():
            try:
                result = await self._primary.classify(request)
            except DetectorUnavailableError as err:
                self._gate.failed(err)
            else:
                self._gate.succeeded()
                return result
        return await self._fallback.classify(request)

    async def aclose(self) -> None:
        await self._primary.aclose()
        await self._fallback.aclose()
