"""Command-line entry point for ``baton``."""

from __future__ import annotations

import asyncio
import getpass
import platform
import shutil
import signal
import subprocess
import sys
from datetime import timedelta
from importlib.metadata import version as package_version
from pathlib import Path
from typing import TYPE_CHECKING, Annotated
from zoneinfo import ZoneInfo

import typer

from baton_herdr.adapters import ADAPTERS
from baton_herdr.budget.choice import choose_agent
from baton_herdr.budget.load import BudgetReader
from baton_herdr.budget.report import budget_lines
from baton_herdr.core.clock import SystemClock
from baton_herdr.core.config import BatonSettings, config_file, user_config_dir
from baton_herdr.core.executables import detector_binary
from baton_herdr.core.logging import configure_logging
from baton_herdr.core.model import AgentKind, OperatorAction, TaskId
from baton_herdr.core.projection import TaskView, project
from baton_herdr.daemon import add_task, open_runtime, poke, run_pending, serve
from baton_herdr.doctor import Level, diagnose, local_timezone, render
from baton_herdr.ledger import open_event_store, open_usage_store
from baton_herdr.policy.load import PolicyError, load_policy
from baton_herdr.projects.create import ProjectSetupError
from baton_herdr.projects.flow import (
    ProjectError,
    approve_plan,
    cancel_project,
    reject_plan,
    start_project,
)
from baton_herdr.scheduler.operator import ActionRefusedError, act, cancel
from baton_herdr.service import (
    BATON_UNIT,
    HERDR_UNIT,
    NEXT_STEPS,
    ServiceError,
    install_units,
    plan_units,
    render_units,
)
from baton_herdr.setup import Io, Paths, SetupError, ask_secret, run_setup

if TYPE_CHECKING:
    from collections.abc import Sequence

app = typer.Typer(name="baton", no_args_is_help=True, add_completion=False)
task_app = typer.Typer(name="task", help="Manage tasks.", no_args_is_help=True)
app.add_typer(task_app)
service_app = typer.Typer(name="service", help="Run batond as a systemd user service.")
app.add_typer(service_app)
project_app = typer.Typer(
    name="project", help="Projects: a request, planned and run step by step.", no_args_is_help=True
)
app.add_typer(project_app)
policy_app = typer.Typer(
    name="policy", help="Which agent commands run without asking you.", no_args_is_help=True
)
app.add_typer(policy_app)


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

    This is the detector's NDJSON contract (schemas/detector-*.v1.json), answered by
    ``baton-detect classify`` ([detector] binary), as batond does.
    """
    settings = _settings()
    binary = detector_binary(settings.detector.binary)
    if binary is None:
        typer.echo(f"{settings.detector.binary} not found; see `baton doctor`.", err=True)
        raise typer.Exit(code=2)
    # One line at a time, so each answer comes as soon as its request is read.
    with subprocess.Popen(  # noqa: S603 - the configured classifier, fixed arguments
        [str(binary), "classify"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as process:
        if process.stdin is None or process.stdout is None:  # pragma: no cover - PIPE
            raise typer.Exit(code=2)
        for line in sys.stdin:
            if not line.strip():
                continue
            process.stdin.write(line if line.endswith("\n") else line + "\n")
            process.stdin.flush()
            answer = process.stdout.readline()
            if not answer:
                break
            sys.stdout.write(answer)
            sys.stdout.flush()
        process.stdin.close()
        code = process.wait()
        if code and process.stderr is not None:
            typer.echo(process.stderr.read().strip(), err=True)
    raise typer.Exit(code=code)


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
    poke(settings.database.path)  # a running batond picks it up now


@task_app.command("cancel")
def task_cancel(
    task_id: Annotated[str, typer.Argument(help="The task to drop.")],
    reason: Annotated[str | None, typer.Option("--reason", help="Kept in the log.")] = None,
) -> None:
    """Cancel an open task, and close its agent's pane."""
    settings = _settings()

    async def drop() -> str:
        async with open_runtime(settings) as runtime:
            try:
                closed = await cancel(
                    store=runtime.store,
                    host=runtime.host,
                    clock=runtime.clock,
                    task_id=TaskId(task_id),
                    by="cli",
                    reason=reason or "",
                )
            except ActionRefusedError as err:
                typer.echo(str(err), err=True)
                raise typer.Exit(code=1) from err
        return f"{task_id} cancelled" + ("; its pane is closed" if closed else "")

    typer.echo(asyncio.run(drop()))
    poke(settings.database.path)


@project_app.command("new")
def project_new(
    request: Annotated[str, typer.Argument(help="What to build, as you would say it.")],
) -> None:
    """Start a project: a new directory, and an agent to plan it (ADR 0016)."""
    settings = _settings()

    async def start() -> str:
        store = await open_event_store(settings.database.path)
        try:
            project_id, task_id, workdir = await start_project(
                store, root=settings.projects.root, request=request, clock=SystemClock()
            )
        except (ProjectError, ProjectSetupError) as err:
            typer.echo(str(err), err=True)
            raise typer.Exit(code=1) from err
        finally:
            await store.close()
        return f"{project_id}  {workdir}\nplan task {task_id}"

    typer.echo(asyncio.run(start()))
    poke(settings.database.path)


def _plan_choice(task_id: str, *, start: bool) -> None:
    settings = _settings()

    async def choose() -> str:
        store = await open_event_store(settings.database.path)
        try:
            if start:
                steps = await approve_plan(store, TaskId(task_id), by="cli", clock=SystemClock())
                return f"started: {len(steps)} steps ({', '.join(steps)})"
            await reject_plan(store, TaskId(task_id), by="cli", clock=SystemClock())
            return "plan cancelled"
        except ProjectError as err:
            typer.echo(str(err), err=True)
            raise typer.Exit(code=1) from err
        finally:
            await store.close()

    typer.echo(asyncio.run(choose()))
    poke(settings.database.path)


@project_app.command("start")
def project_start(
    task_id: Annotated[str, typer.Argument(help="The plan task whose plan to start.")],
) -> None:
    """Start a proposed plan: its steps run one after another."""
    _plan_choice(task_id, start=True)


@project_app.command("cancel")
def project_cancel(
    target: Annotated[
        str, typer.Argument(help="A project (p-…), or the plan task of a plan waiting for you.")
    ],
) -> None:
    """Drop a project: its waiting plan, and every open task of it, panes closed."""
    if not target.startswith("p-"):
        _plan_choice(target, start=False)
        return
    settings = _settings()

    async def drop() -> str:
        async with open_runtime(settings) as runtime:
            try:
                count = await cancel_project(
                    runtime.store, runtime.host, project_id=target, by="cli", clock=runtime.clock
                )
            except ProjectError as err:
                typer.echo(str(err), err=True)
                raise typer.Exit(code=1) from err
        return f"{target} cancelled: {count} task(s) dropped"

    typer.echo(asyncio.run(drop()))
    poke(settings.database.path)


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


@policy_app.command("check")
def policy_check(
    command: Annotated[str, typer.Argument(help="A shell command, as an agent would ask.")],
    agent: Annotated[
        str, typer.Option(help="The agent asking: claude, codex or opencode.")
    ] = "claude",
    directory: Annotated[
        Path | None,
        typer.Option("--dir", "-C", help="The task's directory (default: the current one)."),
    ] = None,
) -> None:
    """Say what the policy would do if AGENT asked to run COMMAND in DIR (ADR 0013)."""
    settings = _settings()
    try:
        kind = AgentKind(agent)
    except ValueError:
        typer.echo(f"Unknown agent {agent!r}.", err=True)
        raise typer.Exit(2) from None
    if not settings.policy.enabled:
        typer.echo("ask: the policy is off ([policy] enabled = false); a person decides.")
        return
    try:
        policy = load_policy(settings.policy.path)
    except PolicyError as err:
        typer.echo(str(err), err=True)
        raise typer.Exit(1) from None
    workdir = str((directory or Path.cwd()).expanduser().resolve())
    verdict = policy.decide(kind, command, workdir)
    typer.echo(f"{verdict.decision.value}: {verdict.reason}")
    typer.echo(f"rule: {verdict.rule.label if verdict.rule else 'none'}")
    typer.echo(f"policy: {policy.source}")


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
        async with open_runtime(settings, announce=True) as runtime:
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
    poke(settings.database.path)  # batond carries the attempt on at once


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


@app.command()
def setup(
    *,
    service: Annotated[
        bool, typer.Option(help="Install and start baton as a systemd user service.")
    ] = True,
) -> None:
    """Set baton up on this machine, step by step: baton-detect, herdr's integrations, the
    config, Telegram (it finds your chat itself), the policy, and the service. Safe to run
    again: what exists is kept unless you say otherwise."""
    io = Io(
        say=typer.echo,
        ask=lambda question, default: str(typer.prompt(question, default=default)),
        confirm=lambda question, default: typer.confirm(question, default=default),
        secret=ask_secret,
        run=_run_command,
        which=shutil.which,
    )
    directory = user_config_dir()
    settings = _settings()
    paths = Paths(
        config=config_file(),
        env=directory / ".env",
        policy=settings.policy.path,
        projects_root=settings.projects.root,
        local_bin=Path.home() / ".local" / "bin",
    )
    arch = {"arm64": "aarch64"}.get(platform.machine(), platform.machine())
    try:
        complete = run_setup(
            io,
            paths,
            timezone=local_timezone() or "UTC",
            target=f"{arch}-unknown-linux-gnu",
        )
        if service:
            _setup_service(io)
    except SetupError as err:
        typer.echo(str(err), err=True)
        raise typer.Exit(code=1) from err
    typer.echo("\nbaton doctor:")
    checks = asyncio.run(diagnose())
    typer.echo(render(checks))
    if not complete:
        typer.echo("\nSome steps were left for later; run baton setup again to finish them.")


def _run_command(command: Sequence[str]) -> tuple[int, str]:
    try:
        done = subprocess.run(  # noqa: S603 - fixed commands, no shell
            list(command), capture_output=True, text=True, timeout=120, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as err:
        return 1, str(err)
    return done.returncode, done.stdout + done.stderr


def _setup_service(io: Io) -> None:
    """The systemd user units, enabled and (re)started, kept running after logout."""
    if not io.confirm("Run baton as a service (starts at login, keeps running)?", default=True):
        return
    settings = _settings()
    plan = plan_units(settings, config=config_file())
    try:
        written = install_units(plan, force=False)
    except ServiceError:
        if not io.confirm("The service is installed already. Update it?", default=True):
            return
        written = install_units(plan, force=True)
    for path in written:
        io.say(f"Wrote {path}.")
    for command in (
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", HERDR_UNIT, BATON_UNIT],
        ["systemctl", "--user", "restart", HERDR_UNIT, BATON_UNIT],
        ["loginctl", "enable-linger", getpass.getuser()],
    ):
        code, output = io.run(command)
        if code != 0:
            io.say(f"{' '.join(command)} failed: {output.strip()}")
    io.say(f"batond is running. Watch its agents with: herdr --session {plan.session}")


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
