"""Command-line entry point for ``coban``."""

from __future__ import annotations

import asyncio
import signal
import sys
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from coban.adapters import ADAPTERS
from coban.core.config import CobanSettings, config_file
from coban.core.detection import DetectionRequest, DetectionResult
from coban.core.logging import configure_logging
from coban.core.model import OperatorAction, TaskId
from coban.core.projection import TaskView, project
from coban.daemon import add_task, open_runtime, run_pending, serve
from coban.doctor import Level, diagnose, render
from coban.ledger import open_event_store
from coban.scheduler.operator import ActionRefusedError, act
from coban.service import (
    COBAN_UNIT,
    DEFAULT_SESSION,
    NEXT_STEPS,
    ServiceError,
    install_units,
    plan_units,
    render_units,
)

app = typer.Typer(name="coban", no_args_is_help=True, add_completion=False)
task_app = typer.Typer(name="task", help="Manage tasks.", no_args_is_help=True)
app.add_typer(task_app)
service_app = typer.Typer(name="service", help="Run cobanD as a systemd user service.")
app.add_typer(service_app)


def _settings() -> CobanSettings:
    settings = CobanSettings()
    configure_logging(settings.logging.level)
    return settings


@app.callback()
def main() -> None:
    """Orchestrate coding agents running on herdr."""


@app.command()
def version() -> None:
    """Print the coban version."""
    typer.echo(f"coban {package_version('coban')}")


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
        adapter = ADAPTERS.get(request.agent)
        result = (
            adapter.classify(request)
            if adapter is not None
            else DetectionResult(state=request.host_state, evidence="coban:no-adapter")
        )
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
def daemon(
    idle: Annotated[float, typer.Option(help="Seconds between checks for new tasks.")] = 30,
) -> None:
    """Run cobanD: keep running tasks, resume them after limits reset. Ctrl+C stops."""
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
    session: Annotated[
        str, typer.Option(help="herdr session the service runs; attach to it to watch.")
    ] = DEFAULT_SESSION,
    force: Annotated[bool, typer.Option(help="Replace existing unit files.")] = False,
    dry_run: Annotated[bool, typer.Option(help="Print the units instead of writing them.")] = False,
) -> None:
    """Write systemd user units for cobanD and its own herdr server (not enabled)."""
    settings = _settings()
    try:
        plan = plan_units(settings, session=session, config=config_file())
        if dry_run:
            for name, text in render_units(plan).items():
                typer.echo(f"# {name}\n{text}")
            return
        for path in install_units(plan, force=force):
            typer.echo(f"wrote {path}")
    except ServiceError as err:
        typer.echo(str(err), err=True)
        raise typer.Exit(code=1) from err
    typer.echo(NEXT_STEPS.format(coban_unit=COBAN_UNIT, session=session))


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
