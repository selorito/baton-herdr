"""Supervise ``baton-detect usage`` and store its NDJSON in the usage tables.

The binary follows the agents' logs and writes one JSON object per line. Each line is
validated against the contract (``baton_herdr.core.usage``) and stored; lines are
written in batches, and anything already stored is dropped by the database, so the
binary is started again with a generous ``--since``.

When the binary is missing the collector says so once and looks again later; when
it exits it is started again, waiting longer after each quick failure.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import ValidationError

from baton_herdr.core.logging import get_logger
from baton_herdr.core.usage import USAGE_EVENT

if TYPE_CHECKING:
    from baton_herdr.core.config import UsageSettings
    from baton_herdr.core.ports import UsageStore
    from baton_herdr.core.usage import UsageEvent

# Lines are stored this many at a time, or after this long without a new one.
BATCH_SIZE = 500
BATCH_WAIT_S = 0.5


@dataclass(frozen=True, slots=True)
class Backoff:
    first_s: float = 1
    max_s: float = 60
    # A run this long counts as healthy: the next failure starts from first_s again.
    healthy_s: float = 60


def detector_binary(name: str) -> Path | None:
    """``name`` as a path to an executable, or found on PATH."""
    if os.sep in name:
        path = Path(name).expanduser()
        return path if path.is_file() and os.access(path, os.X_OK) else None
    found = shutil.which(name)
    return Path(found) if found else None


class UsageCollector:
    def __init__(
        self,
        *,
        store: UsageStore,
        settings: UsageSettings,
        backoff: Backoff | None = None,
    ) -> None:
        self._store = store
        self._settings = settings
        self._backoff = backoff or Backoff()
        self._log = get_logger("baton.collector")
        self.stored = 0  # new records since start, for tests and status

    async def run(self, stop: asyncio.Event) -> None:
        """Keep ``baton-detect usage`` running until ``stop`` is set."""
        delay = self._backoff.first_s
        missing_reported = False
        while not stop.is_set():
            binary = detector_binary(self._settings.binary)
            if binary is None:
                if not missing_reported:
                    self._log.warning(
                        "baton-detect not found; usage is not collected",
                        binary=self._settings.binary,
                        hint="cargo install --path crates/baton-detect --locked",
                    )
                    missing_reported = True
                await _wait(stop, self._backoff.max_s)
                continue
            missing_reported = False
            loop = asyncio.get_running_loop()
            started = loop.time()
            code = await self._run_once(binary, stop)
            if stop.is_set():
                break
            if loop.time() - started >= self._backoff.healthy_s:
                delay = self._backoff.first_s
            self._log.warning("baton-detect exited; starting it again", code=code, after_s=delay)
            await _wait(stop, delay)
            delay = min(delay * 2, self._backoff.max_s)

    async def _arguments(self) -> list[str]:
        arguments = ["usage"]
        latest = await self._store.latest_at()
        if latest is not None:
            since = latest - timedelta(minutes=self._settings.reread_minutes)
            arguments += ["--since", since.isoformat()]
        for option, path in (
            ("--claude-dir", self._settings.claude_dir),
            ("--codex-dir", self._settings.codex_dir),
            ("--opencode-db", self._settings.opencode_db),
        ):
            if path is not None:
                arguments += [option, str(path)]
        return arguments

    async def _run_once(self, binary: Path, stop: asyncio.Event) -> int | None:
        arguments = await self._arguments()
        self._log.info("starting baton-detect", binary=str(binary), arguments=arguments)
        process = await asyncio.create_subprocess_exec(
            binary,
            *arguments,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        if process.stdout is None or process.stderr is None:
            msg = "baton-detect started without pipes"
            raise RuntimeError(msg)
        consume = asyncio.create_task(self._consume(process.stdout))
        errors = asyncio.create_task(self._log_stderr(process.stderr))
        stopped = asyncio.create_task(stop.wait())
        tasks = [consume, errors, stopped]
        try:
            await asyncio.wait([consume, stopped], return_when=asyncio.FIRST_COMPLETED)
            if consume.done():
                # It closed stdout: let it finish saying why on stderr.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(asyncio.shield(errors), 2)
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 5)
                except TimeoutError:
                    process.kill()
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        return await process.wait()

    async def _consume(self, stdout: asyncio.StreamReader) -> None:
        batch: list[UsageEvent] = []
        while True:
            try:
                line = await asyncio.wait_for(stdout.readline(), BATCH_WAIT_S)
            except TimeoutError:
                await self._flush(batch)
                continue
            if not line:  # the process closed stdout
                await self._flush(batch)
                return
            try:
                batch.append(USAGE_EVENT.validate_json(line))
            except ValidationError as err:
                self._log.warning(
                    "baton-detect line off contract; skipped",
                    error=str(err.errors()[0]["msg"]),
                    line=line[:200].decode(errors="replace"),
                )
            if len(batch) >= BATCH_SIZE:
                await self._flush(batch)

    async def _flush(self, batch: list[UsageEvent]) -> None:
        if not batch:
            return
        new = await self._store.record(batch)
        self.stored += new
        self._log.debug("usage stored", lines=len(batch), new=new)
        batch.clear()

    async def _log_stderr(self, stderr: asyncio.StreamReader) -> None:
        async for line in stderr:
            self._log.warning("baton-detect", message=line.decode(errors="replace").rstrip())


async def _wait(stop: asyncio.Event, seconds: float) -> None:
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), seconds)
