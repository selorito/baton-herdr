"""Command-line entry point for ``baton``."""

from __future__ import annotations

import asyncio
import signal
import sys
from datetime import timedelta
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

import typer
from pydantic import ValidationError

from baton_herdr.adapters import ADAPTERS
from baton_herdr.budget.choice import choose_agent
from baton_herdr.budget.load import BudgetReader
from baton_herdr.budget.report import budget_lines
from baton_herdr.core.clock import SystemClock
from baton_herdr.core.config import BatonSettings, config_file
from baton_herdr.core.detection import DetectionRequest
from baton_herdr.core.detector import classify_with
from baton_herdr.core.logging import configure_logging
from baton_herdr.core.model import AgentKind, OperatorAction, TaskId
from baton_herdr.core.projection import TaskView, project
from baton_herdr.daemon import add_task, open_runtime, run_pending, serve
from baton_herdr.doctor import Level, diagnose, render
from baton_herdr.ledger import open_event_store, open_usage_store
from baton_herdr.scheduler.operator import ActionRefusedError, act
from baton_herdr.service import (
    BATON_UNIT,
    NEXT_STEPS,
    ServiceError,
    install_units,
    plan_units,
    render_units,
)

app = typer.Typer(name="baton", no_args_is_help=True, add_completion=False)
task_app = typer.Typer(name="task", help="Manage tasks.", no_args_is_help=True)
app.add_typer(task_app)
service_app = typer.Typer(name="service", help="Run batond as a systemd user service.")
app.add_typer(service_app)


def _settings() -> BatonSettings:
    settings = BatonSettings()
    configure_logging(settings.logging.level)
    return settings


@app.callback()
def main() -> None:
    """Orchestrate coding agents running on herdr."""


@app.command()
def version() -> None:
    """Print the baton version."""
    typer.echo(f"baton {package_version('baton-herdr')}")


@app.command()
def detect() -> None:
    """Classify screens: one DetectionRequest JSON per stdin line, one result per stdout line.

    This is the detector's NDJSON contract (schemas/detector-*.v1.json).
    """
    for number, line in enumerate(sys.stdin, start=1):
        if not line.strip():
            continue
        try:
            request = DetectionRequest.model_validate_json(line)
        except ValidationError as err:
            typer.echo(f"line {number}: invalid detection request: {err}", err=True)
            raise typer.Exit(code=2) from err
        result = classify_with(ADAPTERS, request)
        sys.stdout.write(result.model_dump_json() + "\n")
        sys.stdout.flush()


@task_app.command("add")
def task_add(
    title: Annotated[str, typer.Argument(help="Short name shown in status and notices.")],
    instructions: Annotated[
        str | None, typer.Option("--instructions", "-i", help="The prompt for the agent.")
    ] = None,
    instructions_file: Annotated[
        Path | None, typer.Option("--instructions-file", "-f", exists=True, dir_okay=False)
    ] = None,
    workdir: Annotated[
        Path, typer.Option("--workdir", "-C", exists=True, file_okay=False)
    ] = Path(),
) -> None:
    """Add a task. Prints its id."""
    if (instructions is None) == (instructions_file is None):
        typer.echo("give exactly one of --instructions or --instructions-file", err=True)
        raise typer.Exit(code=2)
    text = instructions or Path(instructions_file or "").read_text(encoding="utf-8")
    settings = _settings()

    async def add() -> str:
        store = await open_event_store(settings.database.path)
        try:
            return await add_task(
                store, title=title, instructions=text, workdir=str(workdir.resolve())
            )
        finally:
            await store.close()

    typer.echo(asyncio.run(add()))


@app.command()
def status() -> None:
    """Show every task and its attempts."""
    settings = _settings()

    async def board() -> list[TaskView]:
        store = await open_event_store(settings.database.path)
        try:
            return list(project(await store.read()).tasks.values())
        finally:
            await store.close()

    tasks = asyncio.run(board())
    if not tasks:
        typer.echo("no tasks")
    for task in tasks:
        attempts = " ".join(
            f"{a.agent.value}:{a.outcome.value if a.outcome else a.status.value}"
            for a in task.attempts
        )
        typer.echo(f"{task.task_id}  {task.status.value:<11}  {task.title}  {attempts}".rstrip())


@app.command()
def budget() -> None:
    """Show what each agent has left and which one a new task would go to.

    Codex's figures are its own; Claude's are baton's estimate (ADR 0012).
    """
    settings = _settings()
    agents = tuple(AgentKind(agent) for agent in settings.scheduler.agents)
    zone = ZoneInfo(settings.scheduler.timezone)

    async def read() -> list[str]:
        store = await open_event_store(settings.database.path)
        usage = await open_usage_store(settings.database.path)
        try:
            reader = BudgetReader(
                usage=usage,
                cooldown=timedelta(minutes=settings.scheduler.limit_cooldown_minutes),
                claude_window_tokens=settings.budget.claude_window_tokens,
            )
            now = SystemClock().now()
            budgets = await reader.read(agents, await store.read(), now)
        finally:
            await usage.close()
            await store.close()
        choice = choose_agent(
            [a for a in agents if a in ADAPTERS],
            budgets,
            reserve_percent=settings.budget.reserve_percent,
            zone=zone,
        )
        return [
            *budget_lines(budgets.values(), now=now, zone=zone),
            "",
            f"Next task: {choice.reason}",
        ]

    for line in asyncio.run(read()):
        typer.echo(line)


@app.command()
def daemon(
    idle: Annotated[float, typer.Option(help="Seconds between checks for new tasks.")] = 30,
) -> None:
    """Run batond: keep running tasks, resume them after limits reset. Ctrl+C stops."""
    settings = _settings()

    async def serve_until_interrupted() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signal_number in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signal_number, stop.set)
        async with open_runtime(settings) as runtime:
            await serve(runtime, stop=stop, idle_s=idle)

    asyncio.run(serve_until_interrupted())


def _act(task_id: str, action: OperatorAction, text: str = "") -> None:
    settings = _settings()

    async def carry_out() -> str:
        async with open_runtime(settings) as runtime:
            try:
                blocker = await act(
                    store=runtime.store,
                    host=runtime.host,
                    adapters=ADAPTERS,
                    clock=runtime.clock,
                    task_id=TaskId(task_id),
                    action=action,
                    by="cli",
                    text=text,
                )
            except ActionRefusedError as err:
                typer.echo(str(err), err=True)
                raise typer.Exit(code=1) from err
            return f"{task_id}: {action.value} sent ({blocker.state.value} #{blocker.seq})"

    typer.echo(asyncio.run(carry_out()))


@app.command()
def approve(task_id: Annotated[str, typer.Argument(help="The waiting task.")]) -> None:
    """Approve the permission prompt the task's agent is waiting on."""
    _act(task_id, OperatorAction.APPROVE)


@app.command()
def deny(task_id: Annotated[str, typer.Argument(help="The waiting task.")]) -> None:
    """Deny the permission prompt the task's agent is waiting on."""
    _act(task_id, OperatorAction.DENY)


@app.command()
def answer(
    task_id: Annotated[str, typer.Argument(help="The waiting task.")],
    text: Annotated[str, typer.Argument(help="Your answer to the agent's question.")],
) -> None:
    """Answer the question the task's agent asked."""
    _act(task_id, OperatorAction.ANSWER, text)


@app.command()
def doctor() -> None:
    """Check config, database, herdr, agents, timezone and Telegram; say how to fix problems."""
    checks = asyncio.run(diagnose())
    typer.echo(render(checks))
    if any(check.level is Level.FAIL for check in checks):
        raise typer.Exit(code=1)


@service_app.command("install")
def service_install(
    *,
    force: Annotated[bool, typer.Option(help="Replace existing unit files.")] = False,
    dry_run: Annotated[bool, typer.Option(help="Print the units instead of writing them.")] = False,
) -> None:
    """Write systemd user units for batond and its own herdr server (not enabled).

    The herdr session is [herdr] session from the settings (default "baton"), the
    same one the CLI and `baton doctor` connect to.
    """
    settings = _settings()
    try:
        plan = plan_units(settings, config=config_file())
        if dry_run:
            for name, text in render_units(plan).items():
                typer.echo(f"# {name}\n{text}")
            return
        for path in install_units(plan, force=force):
            typer.echo(f"wrote {path}")
    except ServiceError as err:
        typer.echo(str(err), err=True)
        raise typer.Exit(code=1) from err
    typer.echo(NEXT_STEPS.format(baton_unit=BATON_UNIT, session=plan.session))


@app.command()
def run() -> None:
    """Run every task that is open and has no live attempt, once, then exit."""
    settings = _settings()

    async def run_all() -> dict[str, str]:
        async with open_runtime(settings) as runtime:
            return {k: v.value for k, v in (await run_pending(runtime)).items()}

    results = asyncio.run(run_all())
    if not results:
        typer.echo("nothing to run")
    for task_id, task_status in results.items():
        typer.echo(f"{task_id}  {task_status}")
