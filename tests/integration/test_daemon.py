"""batond wiring and the CLI, on a real SQLite store with simulated agents."""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from baton_herdr.cli import app
from baton_herdr.core.config import (
    BatonSettings,
    DatabaseSettings,
    DetectorSettings,
    SchedulerSettings,
    UsageSettings,
)
from baton_herdr.core.fakes import FixedClock, RecordingNotifier
from baton_herdr.core.model import AgentKind, TaskId, TaskStatus
from baton_herdr.core.notify import NoticeKind
from baton_herdr.core.projection import project
from baton_herdr.daemon import (
    add_task,
    claude_status_line,
    open_runtime,
    run_pending,
    runner_settings,
    serve,
)
from baton_herdr.ledger import open_event_store

from detector_binary import detector
from simulated_agents import SimulatedAgents

if TYPE_CHECKING:
    from pathlib import Path

    from baton_herdr.scheduler.queue import Signature

NOW = datetime(2026, 10, 2, 9, 0, tzinfo=UTC)
cli = CliRunner()


def settings_for(
    db: Path, usage: UsageSettings | None = None, **scheduler: object
) -> BatonSettings:
    return BatonSettings(
        database=DatabaseSettings(path=db),
        detector=DetectorSettings(binary=str(detector())),
        # Never a real baton-detect here: it would read this machine's agent logs.
        usage=usage or UsageSettings(enabled=False),
        scheduler=SchedulerSettings.model_validate(
            {"start_timeout_seconds": 2, "turn_timeout_seconds": 2, "poll_interval_seconds": 0.05}
            | scheduler
        ),
    )


async def test_pending_tasks_run_in_order_and_the_result_is_persisted(tmp_path: Path) -> None:
    db = tmp_path / "baton.db"
    settings = settings_for(db)
    clock = FixedClock(NOW)
    store = await open_event_store(db)
    first = await add_task(store, title="one", instructions="do one", workdir="/w", clock=clock)
    second = await add_task(store, title="two", instructions="do two", workdir="/w", clock=clock)
    await store.close()

    notifier = RecordingNotifier()
    async with open_runtime(
        settings, host=SimulatedAgents(clock), notifier=notifier, clock=clock
    ) as runtime:
        results = await run_pending(runtime)

    # The first task limits Claude and moves to Codex; the second goes straight to Codex.
    assert results == {first: TaskStatus.COMPLETED, second: TaskStatus.COMPLETED}
    assert notifier.kinds.count(NoticeKind.TASK_HANDED_OFF) == 1
    reopened = await open_event_store(db)
    try:
        board = project(await reopened.read())
    finally:
        await reopened.close()
    assert [a.agent for a in board.tasks[second].attempts] == [AgentKind.CODEX]


def test_runner_settings_come_from_the_scheduler_section() -> None:
    settings = SchedulerSettings(
        agents=("codex",),
        limit_cooldown_minutes=5,
        launch_commands={"codex": "/opt/bin/codex --profile ci"},
    )
    converted = runner_settings(settings)
    assert converted.agents == (AgentKind.CODEX,)
    assert converted.limit_cooldown.total_seconds() == 300
    assert converted.launch_commands == {AgentKind.CODEX: "/opt/bin/codex --profile ci"}


def test_claudes_status_line_is_set_only_while_usage_is_collected(tmp_path: Path) -> None:
    binary = tmp_path / "baton-detect"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    log = tmp_path / "claude-status.jsonl"
    on = UsageSettings(binary=str(binary), claude_status_log=log)
    (args,) = claude_status_line(on).values()
    assert args.startswith("--settings ")
    assert str(log) in args
    assert claude_status_line(on.model_copy(update={"enabled": False})) == {}
    missing = on.model_copy(update={"binary": str(tmp_path / "nowhere")})
    assert claude_status_line(missing) == {}  # nothing to run: Claude's footer stays as it is


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    return {"BATON_DATABASE__PATH": str(tmp_path / "cli.db"), "BATON_LOGGING__LEVEL": "warning"}


def test_cli_adds_tasks_and_shows_their_status(tmp_path: Path, env: dict[str, str]) -> None:
    added = cli.invoke(
        app, ["task", "add", "power", "-i", "Add power()", "-C", str(tmp_path)], env=env
    )
    assert added.exit_code == 0, added.output
    task_id = added.stdout.strip()
    assert task_id.startswith("t-")

    shown = cli.invoke(app, ["status"], env=env)
    assert shown.exit_code == 0
    assert shown.stdout.split() == [task_id, "pending", "power"]


@pytest.mark.parametrize("args", [[], ["-i", "x", "-f", "README.md"]])
def test_cli_needs_exactly_one_source_of_instructions(
    tmp_path: Path, env: dict[str, str], args: list[str]
) -> None:
    (tmp_path / "README.md").write_text("x")
    result = cli.invoke(app, ["task", "add", "t", *args], env=env)
    assert result.exit_code == 2


def test_cli_run_without_agents_leaves_tasks_pending(tmp_path: Path, env: dict[str, str]) -> None:
    cli.invoke(app, ["task", "add", "power", "-i", "x", "-C", str(tmp_path)], env=env)

    # No agent configured: nothing is launched and herdr is never contacted.
    result = cli.invoke(app, ["run"], env=env | {"BATON_SCHEDULER__AGENTS": "[]"})

    assert result.exit_code == 0, result.output
    assert result.stdout.split()[1] == "pending"


async def test_the_daemon_waits_quietly_and_resumes_after_the_reset(tmp_path: Path) -> None:
    db = tmp_path / "baton.db"
    clock = FixedClock(NOW)
    store = await open_event_store(db)
    limited = await add_task(store, title="one", instructions="do one", workdir="/w", clock=clock)
    await store.close()
    notifier = RecordingNotifier()
    host = SimulatedAgents(clock)
    seen: dict[TaskId, Signature] = {}

    async with open_runtime(
        settings_for(db, agents=["claude"]), host=host, notifier=notifier, clock=clock
    ) as runtime:
        assert await run_pending(runtime, seen=seen) == {limited: TaskStatus.WAITING}
        notices = len(notifier.notices)

        # Nothing changed: the waiting task is not run or reported again.
        assert await run_pending(runtime, seen=seen) == {}
        assert len(notifier.notices) == notices

        # A task added meanwhile is picked up; with Claude limited it waits too.
        later = await add_task(
            runtime.store, title="two", instructions="do two", workdir="/w", clock=clock
        )
        assert await run_pending(runtime, seen=seen) == {later: TaskStatus.PENDING}

        # The reset passes: the first task resumes its session and finishes; the second
        # starts a fresh session, which in this simulation hits the limit again.
        clock.advance(timedelta(minutes=31))
        assert await run_pending(runtime, seen=seen) == {
            limited: TaskStatus.COMPLETED,
            later: TaskStatus.WAITING,
        }
    assert host.launched == ["claude", "claude --resume claude-session", "claude"]


async def test_serve_stops_when_asked(tmp_path: Path) -> None:
    clock = FixedClock(NOW)
    stop = asyncio.Event()
    async with open_runtime(
        settings_for(tmp_path / "baton.db"),
        host=SimulatedAgents(clock),
        notifier=RecordingNotifier(),
        clock=clock,
    ) as runtime:
        task = asyncio.create_task(serve(runtime, stop=stop, idle_s=0.01))
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, timeout=2)


async def test_an_operator_action_wakes_the_loop_before_its_idle_time(tmp_path: Path) -> None:
    clock = FixedClock(NOW)
    stop = asyncio.Event()
    async with open_runtime(
        settings_for(tmp_path / "baton.db"),
        host=SimulatedAgents(clock),
        notifier=RecordingNotifier(),
        clock=clock,
    ) as runtime:
        serving = asyncio.create_task(serve(runtime, stop=stop, idle_s=60))
        await asyncio.sleep(0.05)  # first cycle done; now asleep for a minute
        task_id = await add_task(
            runtime.store, title="late", instructions="do it", workdir="/w", clock=clock
        )
        runtime.wake.set()
        for _ in range(100):
            if project(await runtime.store.read()).tasks[task_id].attempts:
                break
            await asyncio.sleep(0.02)
        stop.set()
        await asyncio.wait_for(serving, timeout=5)
        assert project(await runtime.store.read()).tasks[task_id].attempts


async def test_batond_collects_usage_while_it_serves(tmp_path: Path) -> None:
    lines = tmp_path / "lines.ndjson"
    lines.write_text(
        '{"kind":"usage","agent":"codex","session_id":"c","record_id":"codex:c:1",'
        '"at":"2026-10-02T09:00:00.000Z","model":null,'
        '"tokens":{"input":1,"output":2,"cache_read":3,"cache_write":0,"reasoning":0}}\n'
    )
    detector = tmp_path / "baton-detect"
    detector.write_text(
        f"#!{sys.executable}\nimport sys, time\n"
        f"sys.stdout.write(open({str(lines)!r}).read()); sys.stdout.flush(); time.sleep(30)\n"
    )
    detector.chmod(0o755)
    clock = FixedClock(NOW)
    stop = asyncio.Event()
    settings = settings_for(tmp_path / "baton.db", UsageSettings(binary=str(detector)))
    async with open_runtime(
        settings, host=SimulatedAgents(clock), notifier=RecordingNotifier(), clock=clock
    ) as runtime:
        assert runtime.usage is not None
        serving = asyncio.create_task(serve(runtime, stop=stop, idle_s=60))
        async with asyncio.timeout(10):
            while not await runtime.usage.usage():
                await asyncio.sleep(0.02)
        stop.set()
        await asyncio.wait_for(serving, 10)
        assert [r.record_id for r in await runtime.usage.usage()] == ["codex:c:1"]


async def test_a_task_added_from_the_cli_starts_without_waiting_for_the_idle_time(
    tmp_path: Path, env: dict[str, str]
) -> None:
    db = tmp_path / "cli.db"
    clock = FixedClock(NOW)
    stop = asyncio.Event()
    async with open_runtime(
        settings_for(db), host=SimulatedAgents(clock), notifier=RecordingNotifier(), clock=clock
    ) as runtime:
        serving = asyncio.create_task(serve(runtime, stop=stop, idle_s=60))
        await asyncio.sleep(0.05)  # first cycle done; now asleep for a minute
        # The CLI runs its own event loop, as it does from a shell.
        added = await asyncio.to_thread(
            cli.invoke, app, ["task", "add", "late", "-i", "do it", "-C", str(tmp_path)], env=env
        )
        assert added.exit_code == 0, added.output
        task_id = TaskId(added.stdout.strip())
        for _ in range(100):
            if project(await runtime.store.read()).tasks[task_id].attempts:
                break
            await asyncio.sleep(0.02)
        stop.set()
        await asyncio.wait_for(serving, timeout=5)
        assert project(await runtime.store.read()).tasks[task_id].attempts
