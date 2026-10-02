"""batond's side of baton-detect: storing its lines, restarting it, stopping it."""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from baton_herdr.collector.usage import Backoff, UsageCollector
from baton_herdr.core.config import UsageSettings
from baton_herdr.core.fakes import InMemoryUsageStore
from baton_herdr.core.usage import UsageRecord, UsageTokens

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import pytest

QUICK = Backoff(first_s=0.01, max_s=0.05, healthy_s=30)
AT = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)


def line(record_id: str, minute: int = 0) -> str:
    return json.dumps(
        {
            "kind": "usage",
            "agent": "claude",
            "session_id": "s",
            "record_id": record_id,
            "at": f"2026-10-01T10:{minute:02d}:00.000Z",
            "model": "m",
            "tokens": {"input": 1, "output": 2, "cache_read": 3, "cache_write": 4},
        }
    )


def fake_detector(tmp_path: Path, *, lines: list[str], then: str = "exit 0") -> Path:
    """An executable that logs its arguments, prints ``lines`` and then exits, fails or
    waits (``then``: "exit N" or "sleep")."""
    out = tmp_path / "lines.ndjson"
    out.write_text("".join(f"{text}\n" for text in lines))
    script = tmp_path / "baton-detect"
    script.write_text(
        f"#!{sys.executable}\n"
        "import sys, time, pathlib\n"
        f"base = pathlib.Path({str(tmp_path)!r})\n"
        "with open(base / 'calls', 'a') as calls:\n"
        "    calls.write(' '.join(sys.argv[1:]) + '\\n')\n"
        "sys.stdout.write((base / 'lines.ndjson').read_text()); sys.stdout.flush()\n"
        "print('baton-detect: something to say', file=sys.stderr)\n"
        f"then = {then!r}\n"
        "if then == 'sleep':\n"
        "    time.sleep(30)\n"
        "sys.exit(int(then.split()[1]))\n"
    )
    script.chmod(0o755)
    return script


def calls(tmp_path: Path) -> list[str]:
    path = tmp_path / "calls"
    return path.read_text().splitlines() if path.exists() else []


async def until(condition: Callable[[], bool]) -> None:
    async with asyncio.timeout(5):
        while not condition():
            await asyncio.sleep(0.01)


def logged(capsys: pytest.CaptureFixture[str]) -> str:
    # Wherever the logger writes in tests (structlog's default is stdout).
    captured = capsys.readouterr()
    return captured.out + captured.err


async def test_lines_are_stored_once_and_off_contract_lines_skipped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = InMemoryUsageStore()
    await store.record(
        [
            UsageRecord(
                agent="claude",
                session_id="s",
                record_id="claude:old",
                at=AT,
                tokens=UsageTokens(input=1, output=1, cache_read=0, cache_write=0),
            )
        ]
    )
    binary = fake_detector(
        tmp_path,
        lines=[line("claude:old"), line("claude:a", 1), '{"kind":"nonsense"}', line("claude:b", 2)],
        then="sleep",
    )
    settings = UsageSettings(binary=str(binary), claude_dir=tmp_path / "c", reread_minutes=30)
    collector = UsageCollector(store=store, settings=settings, backoff=QUICK)
    stop = asyncio.Event()
    running = asyncio.create_task(collector.run(stop))

    await until(lambda: len(store.records) == 3)
    stop.set()
    await asyncio.wait_for(running, 10)  # the sleeping child is terminated

    assert [r.record_id for r in store.records] == ["claude:old", "claude:a", "claude:b"]
    assert collector.stored == 2
    # Started from 30 minutes before the newest stored record, with the configured path.
    (arguments,) = calls(tmp_path)
    assert arguments == f"usage --since 2026-10-01T09:30:00+00:00 --claude-dir {tmp_path / 'c'}"
    output = logged(capsys)
    assert "line off contract" in output
    assert "something to say" in output


async def test_a_detector_that_exits_is_started_again(tmp_path: Path) -> None:
    store = InMemoryUsageStore()
    binary = fake_detector(tmp_path, lines=[line("claude:a")], then="exit 3")
    collector = UsageCollector(
        store=store, settings=UsageSettings(binary=str(binary)), backoff=QUICK
    )
    stop = asyncio.Event()
    running = asyncio.create_task(collector.run(stop))
    await until(lambda: len(calls(tmp_path)) >= 3)
    stop.set()
    await asyncio.wait_for(running, 10)

    assert [r.record_id for r in store.records] == ["claude:a"]  # stored once
    assert calls(tmp_path)[1].startswith("usage --since ")  # resumed from the store


async def test_a_missing_detector_is_reported_once(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    collector = UsageCollector(
        store=InMemoryUsageStore(),
        settings=UsageSettings(binary=str(tmp_path / "nowhere" / "baton-detect")),
        backoff=Backoff(first_s=0.01, max_s=0.01, healthy_s=30),
    )
    stop = asyncio.Event()
    running = asyncio.create_task(collector.run(stop))
    await asyncio.sleep(0.1)  # several looks
    stop.set()
    await asyncio.wait_for(running, 5)
    assert logged(capsys).count("baton-detect not found") == 1
