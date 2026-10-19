"""batond wiring and the CLI, on a real SQLite store with simulated agents."""

from __future__ import annotations

import asyncio
import json
import shlex
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
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
from baton_herdr.core.events import AttemptInterrupted, TaskCreated
from baton_herdr.core.fakes import FixedClock, RecordingNotifier
from baton_herdr.core.model import (
    AgentKind,
    AgentState,
    AttemptStatus,
    InterruptReason,
    TaskId,
    TaskStatus,
)
from baton_herdr.core.notify import NoticeKind
from baton_herdr.core.panes import PaneObservation, PaneProcess
from baton_herdr.core.projection import project
from baton_herdr.daemon import (
    add_task,
    claude_session_args,
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
    from collections.abc import Sequence

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
            {
                "start_timeout_seconds": 2,
                "turn_timeout_seconds": 2,
                "poll_interval_seconds": 0.05,
                # Lean sessions read ~/.claude; these tests must not depend on this machine's.
                "lean_sessions": False,
            }
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


def test_slots_come_from_the_scheduler_section() -> None:
    converted = runner_settings(SchedulerSettings(agent_slots={"claude": 3}, max_parallel_tasks=5))
    assert converted.agent_slots == {AgentKind.CLAUDE: 3}
    assert converted.default_agent_slots == 2
    assert converted.max_parallel_tasks == 5
    with pytest.raises(ValueError, match="greater than or equal to 1"):
        SchedulerSettings.model_validate({"agent_slots": {"codex": 0}})


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


def test_a_lean_claude_session_gets_one_settings_with_the_status_line_in_it(
    tmp_path: Path,
) -> None:
    binary = tmp_path / "baton-detect"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    usage = UsageSettings(binary=str(binary), claude_status_log=tmp_path / "status.jsonl")
    (args,) = claude_session_args(usage, lean=True, claude_home=tmp_path).values()
    flag, value = shlex.split(args)
    settings = json.loads(value)
    assert flag == "--settings"
    assert settings["disableBundledSkills"] is True
    assert settings["statusLine"]["type"] == "command"
    # Without usage only the lean part is left; with neither, nothing is added.
    (lean_only,) = claude_session_args(
        usage.model_copy(update={"enabled": False}), lean=True, claude_home=tmp_path
    ).values()
    assert "statusLine" not in json.loads(shlex.split(lean_only)[1])
    off = usage.model_copy(update={"enabled": False})
    assert claude_session_args(off, lean=False) == {}


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


def test_cli_cancels_a_task_once(tmp_path: Path, env: dict[str, str]) -> None:
    added = cli.invoke(app, ["task", "add", "x", "-i", "x", "-C", str(tmp_path)], env=env)
    task_id = added.stdout.strip()

    cancelled = cli.invoke(app, ["task", "cancel", task_id, "--reason", "not needed"], env=env)
    assert cancelled.exit_code == 0, cancelled.output
    assert cli.invoke(app, ["status"], env=env).stdout.split() == [task_id, "cancelled", "x"]

    again = cli.invoke(app, ["task", "cancel", task_id], env=env)
    assert again.exit_code == 1
    assert "already cancelled" in again.output
    assert cli.invoke(app, ["task", "cancel", "t-none"], env=env).exit_code == 1


def test_cli_starts_a_project_with_a_plan_task(tmp_path: Path, env: dict[str, str]) -> None:
    env = {**env, "BATON_PROJECTS__ROOT": str(tmp_path / "projects")}
    started = cli.invoke(app, ["project", "new", "A one-page website"], env=env)
    assert started.exit_code == 0, started.output
    assert started.stdout.startswith("p-")
    assert (tmp_path / "projects").is_dir()
    shown = cli.invoke(app, ["status"], env=env).stdout
    assert "pending" in shown
    assert "Plan: A one-page website" in shown


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

        # A task added meanwhile, in another directory, is picked up; with Claude limited
        # it waits too.
        later = await add_task(
            runtime.store, title="two", instructions="do two", workdir="/v", clock=clock
        )
        assert await run_pending(runtime, seen=seen) == {later: TaskStatus.PENDING}

        # The reset passes: the first task resumes its session and finishes; the second
        # starts a fresh session, which in this simulation hits the limit again.
        clock.advance(timedelta(minutes=31))
        assert await run_pending(runtime, seen=seen) == {
            limited: TaskStatus.COMPLETED,
            later: TaskStatus.WAITING,
        }
    # The two run side by side after the reset (ADR 0017), in either order.
    assert host.launched[0] == "claude"
    assert sorted(host.launched[1:]) == ["claude", "claude --resume claude-session"]


async def test_the_task_after_a_finished_one_starts_without_the_idle_wait(
    tmp_path: Path,
) -> None:
    db = tmp_path / "baton.db"
    clock = FixedClock(NOW)
    store = await open_event_store(db)
    first = await add_task(store, title="one", instructions="do one", workdir="/w", clock=clock)
    second = TaskId("t-second")
    await store.append(
        [
            TaskCreated(
                occurred_at=NOW,
                task_id=second,
                title="two",
                instructions="do two",
                workdir="/w",
                after=first,
            )
        ]
    )
    await store.close()

    async with open_runtime(
        settings_for(db, agents=["codex"]),
        host=SimulatedAgents(clock),
        notifier=RecordingNotifier(),
        clock=clock,
    ) as runtime:
        # Two cycles, an hour's idle time: the second cycle must not wait for it.
        serving = serve(runtime, stop=asyncio.Event(), idle_s=3600, max_cycles=2)
        await asyncio.wait_for(serving, timeout=20)
        board = project(await runtime.store.read())
    assert board.tasks[first].status is TaskStatus.COMPLETED
    assert board.tasks[second].status is TaskStatus.COMPLETED


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


async def test_tasks_in_different_directories_run_side_by_side_on_both_agents(
    tmp_path: Path,
) -> None:
    db = tmp_path / "baton.db"
    clock = FixedClock(NOW)
    store = await open_event_store(db)
    ids = [
        await add_task(store, title=n, instructions=f"do {n}", workdir=f"/w/{n}", clock=clock)
        for n in ("one", "two", "three")
    ]
    await store.close()
    host = SimulatedAgents(clock, scripts={AgentKind.CLAUDE: "claude-finish.toml"})
    settings = settings_for(db, agent_slots={"claude": 1, "codex": 1})
    async with open_runtime(
        settings, host=host, notifier=RecordingNotifier(), clock=clock
    ) as runtime:
        results = await run_pending(runtime)
        events = await runtime.store.read()

    assert results == dict.fromkeys(ids, TaskStatus.COMPLETED)
    started = {e.event.task_id: e.seq for e in events if e.event.type == "attempt.started"}
    completed = {e.event.task_id: e.seq for e in events if e.event.type == "task.completed"}
    board = project(events)
    # One slot each: the first two start together, one on each agent; the third takes the
    # first slot that frees.
    assert {board.tasks[i].attempts[0].agent for i in ids[:2]} == {
        AgentKind.CLAUDE,
        AgentKind.CODEX,
    }
    assert max(started[ids[0]], started[ids[1]]) < min(completed.values())
    assert started[ids[2]] > min(completed[ids[0]], completed[ids[1]])


async def test_a_freed_slot_is_filled_without_the_idle_wait(tmp_path: Path) -> None:
    db = tmp_path / "baton.db"
    clock = FixedClock(NOW)
    store = await open_event_store(db)
    ids = [
        await add_task(store, title=n, instructions=f"do {n}", workdir=f"/w/{n}", clock=clock)
        for n in ("one", "two")
    ]
    await store.close()
    stop = asyncio.Event()
    async with open_runtime(
        settings_for(db, agents=["codex"], agent_slots={"codex": 1}),
        host=SimulatedAgents(clock),
        notifier=RecordingNotifier(),
        clock=clock,
    ) as runtime:
        serving = asyncio.create_task(serve(runtime, stop=stop, idle_s=3600))
        async with asyncio.timeout(10):
            while not all(
                t.status is TaskStatus.COMPLETED
                for t in project(await runtime.store.read()).tasks.values()
            ):
                await asyncio.sleep(0.02)
        stop.set()
        await asyncio.wait_for(serving, timeout=5)
        board = project(await runtime.store.read())
    assert [board.tasks[i].status for i in ids] == [TaskStatus.COMPLETED] * 2


class EndlessTurns(SimulatedAgents):
    """Agents that take the prompt and work on it for ever."""

    async def _finish_turn(self, pane_id: str, prompt: str) -> None:
        await asyncio.Event().wait()


async def test_stopping_batond_leaves_every_running_attempt_to_be_reattached(
    tmp_path: Path,
) -> None:
    db = tmp_path / "baton.db"
    clock = FixedClock(NOW)
    store = await open_event_store(db)
    for n in ("one", "two"):
        await add_task(store, title=n, instructions=f"do {n}", workdir=f"/w/{n}", clock=clock)
    await store.close()
    stop = asyncio.Event()
    host = EndlessTurns(clock)
    async with open_runtime(
        settings_for(db, agents=["codex"], turn_timeout_seconds=60),
        host=host,
        notifier=RecordingNotifier(),
        clock=clock,
    ) as runtime:
        serving = asyncio.create_task(serve(runtime, stop=stop, idle_s=3600))
        async with asyncio.timeout(10):
            while len(host.prompts) < 2:
                await asyncio.sleep(0.02)
        stop.set()
        await asyncio.wait_for(serving, timeout=5)
        board = project(await runtime.store.read())
    lives = [t.live_attempt for t in board.tasks.values()]
    assert [a.status for a in lives if a is not None] == [AttemptStatus.ACTIVE] * 2


async def test_each_kind_of_task_runs_on_the_model_set_for_it(tmp_path: Path) -> None:
    db = tmp_path / "baton.db"
    clock = FixedClock(NOW)
    store = await open_event_store(db)
    await add_task(store, title="one", instructions="do one", workdir="/w/one", clock=clock)
    await store.append(
        [
            TaskCreated(
                occurred_at=NOW,
                task_id=TaskId("t-step"),
                title="1/1 Page",
                instructions="write it",
                workdir="/w/step",
                project="p-1",
                role="step",
            )
        ]
    )
    await store.close()
    host = SimulatedAgents(clock)
    settings = settings_for(db, agents=["codex"], models={"codex": {"step": "fast model"}})
    async with open_runtime(settings, host=host, notifier=RecordingNotifier(), clock=clock) as rt:
        await run_pending(rt)
    # The task added by hand keeps the agent's default; the step gets its model.
    assert sorted(host.launched) == ["codex", "codex --model 'fast model'"]


def test_models_come_from_the_scheduler_section() -> None:
    converted = runner_settings(SchedulerSettings(models={"claude": {"step": "sonnet"}}))
    assert converted.models == {AgentKind.CLAUDE: {"step": "sonnet"}}
    with pytest.raises(ValueError, match="Input should be"):
        SchedulerSettings.model_validate({"models": {"claude": {"review": "x"}}})


class CodexFailsToStart(SimulatedAgents):
    """Codex prints an error and is gone at once, as two Codex sessions started in the
    same second did live (its app server's socket); the shell is back in front."""

    async def send_keys(self, pane_id: str, keys: Sequence[str]) -> None:
        command = self._typed.get(pane_id, "")
        if command.startswith("codex") and list(keys) == ["Enter"]:
            self._typed.pop(pane_id)
            self.launched.append(command)
            self.screens[pane_id] = f"$ {command}\nError: remote app server transport failed\n$ "
            self.process_lists[pane_id] = [PaneProcess(pid=7, command="/bin/bash")]
            return
        await super().send_keys(pane_id, keys)


async def test_an_agent_that_exits_as_it_starts_hands_the_task_on_at_once(
    tmp_path: Path,
) -> None:
    db = tmp_path / "baton.db"
    clock = FixedClock(NOW)
    store = await open_event_store(db)
    task_id = await add_task(store, title="t", instructions="do it", workdir="/w", clock=clock)
    await store.close()
    host = CodexFailsToStart(clock, scripts={AgentKind.CLAUDE: "claude-finish.toml"})
    # A start timeout of a minute: the exit must be noticed long before it.
    settings = settings_for(db, agents=["codex", "claude"], start_timeout_seconds=60)
    async with open_runtime(settings, host=host, notifier=RecordingNotifier(), clock=clock) as rt:
        started = asyncio.get_running_loop().time()
        results = await asyncio.wait_for(run_pending(rt), timeout=20)
        took = asyncio.get_running_loop().time() - started
        events = await rt.store.read()

    assert results == {task_id: TaskStatus.COMPLETED}
    assert took < 15
    (interrupted,) = [e.event for e in events if isinstance(e.event, AttemptInterrupted)]
    assert interrupted.reason is InterruptReason.CRASHED
    assert interrupted.detail == "codex exited as it started."
    assert [a.agent for a in project(events).tasks[task_id].attempts] == [
        AgentKind.CODEX,
        AgentKind.CLAUDE,
    ]


class SlowStart(SimulatedAgents):
    """Agents that show up a moment after their command; records when each was typed and
    when it was up."""

    def __init__(self, clock: FixedClock) -> None:
        super().__init__(clock)
        self.typed_at: list[float] = []
        self.up_at: list[float] = []

    async def send_keys(self, pane_id: str, keys: Sequence[str]) -> None:
        if list(keys) == ["Enter"] and self._typed.get(pane_id, "").startswith("codex"):
            loop = asyncio.get_running_loop()
            self.typed_at.append(loop.time())
            await asyncio.sleep(0.3)
            await super().send_keys(pane_id, keys)
            self.up_at.append(loop.time())
            return
        await super().send_keys(pane_id, keys)


async def test_sessions_of_one_agent_start_one_after_another(tmp_path: Path) -> None:
    db = tmp_path / "baton.db"
    clock = FixedClock(NOW)
    store = await open_event_store(db)
    for n in ("one", "two"):
        await add_task(store, title=n, instructions=n, workdir=f"/w/{n}", clock=clock)
    await store.close()
    host = SlowStart(clock)
    async with open_runtime(
        settings_for(db, agents=["codex"]), host=host, notifier=RecordingNotifier(), clock=clock
    ) as rt:
        await run_pending(rt)
    first_up, second_typed = sorted(host.up_at)[0], sorted(host.typed_at)[1]
    assert second_typed >= first_up


SIGN_IN = (
    Path(__file__).parents[2] / "fixtures/codex/signed_out/20261019T223947Z/screen.detection.txt"
)


class CodexSignedOut(SimulatedAgents):
    """Codex shows its sign-in screen, as it did live with a used-up refresh token."""

    async def send_keys(self, pane_id: str, keys: Sequence[str]) -> None:
        command = self._typed.get(pane_id, "")
        if command.startswith("codex") and list(keys) == ["Enter"]:
            self._typed.pop(pane_id)
            self.launched.append(command)
            self.screens[pane_id] = SIGN_IN.read_text()
            self.set_observation(
                PaneObservation(
                    pane_id=pane_id,
                    agent=AgentKind.CODEX,
                    state=AgentState.IDLE,
                    evidence="herdr:rule:osc_title_idle",
                )
            )
            return
        await super().send_keys(pane_id, keys)


async def test_a_signed_out_agent_hands_its_task_on_and_is_left_alone(tmp_path: Path) -> None:
    db = tmp_path / "baton.db"
    clock = FixedClock(NOW)
    store = await open_event_store(db)
    first = await add_task(store, title="one", instructions="x", workdir="/w/1", clock=clock)
    await store.close()
    host = CodexSignedOut(clock, scripts={AgentKind.CLAUDE: "claude-finish.toml"})
    notifier = RecordingNotifier()
    settings = settings_for(db, agents=["codex", "claude"], start_timeout_seconds=60)
    async with open_runtime(settings, host=host, notifier=notifier, clock=clock) as rt:
        assert await asyncio.wait_for(run_pending(rt), 20) == {first: TaskStatus.COMPLETED}
        # A task added later does not try Codex again while it waits for a sign-in.
        second = await add_task(
            rt.store, title="two", instructions="y", workdir="/w/2", clock=clock
        )
        assert await asyncio.wait_for(run_pending(rt), 20) == {second: TaskStatus.COMPLETED}
        events = await rt.store.read()

    board = project(events)
    assert [a.agent for a in board.tasks[first].attempts] == [AgentKind.CODEX, AgentKind.CLAUDE]
    assert [a.agent for a in board.tasks[second].attempts] == [AgentKind.CLAUDE]
    (stopped,) = [n for n in notifier.notices if "is signed out" in n.text]
    assert "codex logout && codex login" in " ".join([stopped.text, *stopped.details])
