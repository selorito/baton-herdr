"""cobanD wiring and the CLI, on a real SQLite store with simulated agents."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from coban.cli import app
from coban.core.config import CobanSettings, DatabaseSettings, SchedulerSettings
from coban.core.fakes import FixedClock, RecordingNotifier
from coban.core.model import AgentKind, TaskStatus
from coban.core.notify import NoticeKind
from coban.core.projection import project
from coban.daemon import add_task, open_runtime, run_pending, runner_settings
from coban.ledger import open_event_store

from simulated_agents import SimulatedAgents

if TYPE_CHECKING:
    from pathlib import Path

NOW = datetime(2026, 10, 2, 9, 0, tzinfo=UTC)
cli = CliRunner()


def settings_for(db: Path, **scheduler: object) -> CobanSettings:
    return CobanSettings(
        database=DatabaseSettings(path=db),
        scheduler=SchedulerSettings.model_validate(
            {"start_timeout_seconds": 2, "turn_timeout_seconds": 2, "poll_interval_seconds": 0.05}
            | scheduler
        ),
    )


async def test_pending_tasks_run_in_order_and_the_result_is_persisted(tmp_path: Path) -> None:
    db = tmp_path / "coban.db"
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
    assert notifier.kinds.count(NoticeKind.AGENT_LIMITED) == 1
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


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    return {"COBAN_DATABASE__PATH": str(tmp_path / "cli.db"), "COBAN_LOGGING__LEVEL": "warning"}


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
    result = cli.invoke(app, ["run"], env=env | {"COBAN_SCHEDULER__AGENTS": "[]"})

    assert result.exit_code == 0, result.output
    assert result.stdout.split()[1] == "pending"
