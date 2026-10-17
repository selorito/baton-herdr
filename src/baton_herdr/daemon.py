"""batond: wires the ports to their real implementations and runs tasks.

This is the composition root. It is the only module that knows which concrete
event store, pane host, adapters and notifier are used; everything below it
depends on the protocols in ``baton_herdr.core``.
"""

from __future__ import annotations

import asyncio
import os
import stat
import uuid
from contextlib import asynccontextmanager, contextmanager, nullcontext, suppress
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING

from baton_herdr.adapters import ADAPTERS
from baton_herdr.adapters.claude import status_line_args
from baton_herdr.budget.availability import fold_availability
from baton_herdr.budget.load import BudgetReader
from baton_herdr.collector import UsageCollector
from baton_herdr.core.clock import SystemClock
from baton_herdr.core.config import BudgetSettings, DetectorSettings
from baton_herdr.core.detector import HostDetector
from baton_herdr.core.events import TaskCreated
from baton_herdr.core.executables import detector_binary
from baton_herdr.core.logging import get_logger
from baton_herdr.core.model import AgentKind, TaskId, TaskStatus
from baton_herdr.core.notify import LoggingNotifier
from baton_herdr.detector import FallbackDetector, ProcessDetector
from baton_herdr.herdr import connect
from baton_herdr.ledger import open_event_store, open_usage_store
from baton_herdr.policy.load import Policy, load_policy
from baton_herdr.projects.deliver import PreviewServer, Showcase, find_browser
from baton_herdr.projects.flow import deliver_projects, halt_stopped_projects, propose_plans
from baton_herdr.scheduler.dispatch import Dispatcher
from baton_herdr.scheduler.queue import Signature, next_wake
from baton_herdr.scheduler.runner import RunnerSettings, TaskRunner
from baton_herdr.telegram.bot import BotSettings, OperatorBot, keep_running, run_bot
from baton_herdr.telegram.notifier import telegram_notifier
from baton_herdr.workdir import GitWorkspace

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator, Mapping
    from pathlib import Path

    from aiogram import Bot

    from baton_herdr.core.config import (
        BatonSettings,
        PolicySettings,
        SchedulerSettings,
        UsageSettings,
    )
    from baton_herdr.core.detector import Detector
    from baton_herdr.core.notify import Notifier
    from baton_herdr.core.panes import PaneHost
    from baton_herdr.core.ports import Clock, EventStore, UsageStore


def make_detector(settings: DetectorSettings) -> Detector:
    """The detector batond classifies screens with (ADR 0011): baton-detect, and herdr's own
    state while it gives no answer."""
    if settings.engine != "rust":
        get_logger("baton.daemon").warning(
            "the Python detector is gone; [detector] engine is treated as rust",
            engine=settings.engine,
        )
    rust = ProcessDetector(settings.binary, timeout_s=settings.timeout_seconds)
    return FallbackDetector(rust, HostDetector())


def make_policy(settings: PolicySettings) -> Policy | None:
    """The permission policy batond applies (ADR 0013); ``None`` when it is off.

    Raises ``PolicyError`` for a policy file that cannot be read, before anything runs.
    """
    return load_policy(settings.path) if settings.enabled else None


def claude_status_line(usage: UsageSettings) -> dict[AgentKind, str]:
    """Claude's status line in the sessions baton starts, while usage is collected and
    baton-detect is there to run it (ADR 0015)."""
    binary = detector_binary(usage.binary) if usage.enabled else None
    if binary is None:
        return {}
    return {AgentKind.CLAUDE: status_line_args(binary, usage.claude_status_log)}


def runner_settings(  # noqa: PLR0913 - the settings sections and what batond adds to them
    settings: SchedulerSettings,
    budget: BudgetSettings | None = None,
    *,
    collecting_usage: bool = False,
    launch_args: Mapping[AgentKind, str] | None = None,
    own_dirs: tuple[str, ...] = (),
    trusted_dirs: tuple[str, ...] = (),
) -> RunnerSettings:
    budget = budget or BudgetSettings()
    return RunnerSettings(
        agents=tuple(AgentKind(agent) for agent in settings.agents),
        limit_cooldown=timedelta(minutes=settings.limit_cooldown_minutes),
        start_timeout_s=settings.start_timeout_seconds,
        turn_timeout_s=settings.turn_timeout_seconds,
        poll_interval_s=settings.poll_interval_seconds,
        stall_after=timedelta(minutes=settings.stall_minutes),
        stall_after_without_usage=timedelta(minutes=settings.stall_minutes_without_usage),
        timezone=settings.timezone,
        launch_commands={AgentKind(a): cmd for a, cmd in settings.launch_commands.items()},
        max_failure_resumes=settings.max_failure_resumes,
        close_finished_panes=settings.close_finished_panes,
        reserve_percent=budget.reserve_percent,
        # Without the collector there is no last response to wait for.
        usage_grace_s=3 if collecting_usage else 0,
        launch_args=launch_args or {},
        own_dirs=own_dirs,
        trusted_dirs=trusted_dirs,
        agent_slots={AgentKind(a): n for a, n in settings.agent_slots.items()},
        max_parallel_tasks=settings.max_parallel_tasks,
    )


@dataclass(frozen=True, slots=True)
class Runtime:
    store: EventStore
    runner: TaskRunner
    settings: RunnerSettings
    clock: Clock
    host: PaneHost
    # Set when something outside the loop (an operator action) wants a cycle now.
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    operator: OperatorBot | None = None
    bot: Bot | None = None
    # Agent usage telemetry (ADR 0011); the collector is None when [usage] is off.
    usage: UsageStore | None = None
    collector: UsageCollector | None = None
    # Remaining budget per agent (ADR 0012), as the scheduler sees it.
    budget: BudgetReader | None = None
    # The FIFO CLI commands write to, to wake batond at once (``poke``); None: not listened to.
    wake_fifo: Path | None = None
    # Where notices go, for what happens outside a task's run: a project's plan (ADR 0016).
    notifier: Notifier | None = None
    # A finished project's page: the preview batond serves, and a browser for its picture.
    preview: PreviewServer | None = None
    browser: Path | None = None


@asynccontextmanager
async def open_runtime(
    settings: BatonSettings,
    *,
    host: PaneHost | None = None,
    notifier: Notifier | None = None,
    clock: Clock | None = None,
    announce: bool = False,
) -> AsyncIterator[Runtime]:
    """Open the store and connect everything; arguments replace the real parts. Only batond
    (``announce``) logs the policy it runs with: the CLI's commands print their own result."""
    policy = make_policy(settings.policy)
    if announce:
        get_logger("baton.daemon").info(
            "permission policy",
            policy=policy.summary() if policy else "off: every permission prompt goes to a person",
            source=policy.source if policy else None,
        )
    store = await open_event_store(settings.database.path)
    usage = await open_usage_store(settings.database.path)
    telegram = telegram_notifier(settings.telegram) if notifier is None else None
    clock = clock or SystemClock()
    scheduling = runner_settings(
        settings.scheduler,
        settings.budget,
        collecting_usage=settings.usage.enabled,
        launch_args=claude_status_line(settings.usage),
        own_dirs=(str(settings.projects.root),),
        trusted_dirs=tuple(str(d) for d in policy.trusted_dirs) if policy else (),
    )
    host = host or connect(settings.herdr)
    wake = asyncio.Event()
    budget = BudgetReader(
        usage=usage,
        cooldown=scheduling.limit_cooldown,
        claude_window_tokens=settings.budget.claude_window_tokens,
    )
    detector = make_detector(settings.detector)
    notices = notifier or telegram or LoggingNotifier()
    try:
        runner = TaskRunner(
            store=store,
            host=host,
            adapters=ADAPTERS,
            notifier=notices,
            clock=clock,
            settings=scheduling,
            budget=budget,
            detector=detector,
            workspace=GitWorkspace(),
            policy=policy,
        )
        operator = None
        if telegram is not None and settings.telegram.owner_id is not None:
            operator = OperatorBot(
                store=store,
                host=host,
                adapters=ADAPTERS,
                clock=clock,
                settings=BotSettings(
                    chat_id=settings.telegram.chat_id or 0,
                    owner_id=settings.telegram.owner_id,
                    agents=scheduling.agents,
                    limit_cooldown=scheduling.limit_cooldown,
                    timezone=scheduling.timezone,
                ),
                budget=budget,
                on_action=wake.set,
                projects_root=settings.projects.root,
            )
        yield Runtime(
            store=store,
            runner=runner,
            settings=scheduling,
            clock=clock,
            host=host,
            wake=wake,
            operator=operator,
            bot=telegram.bot if telegram is not None else None,
            usage=usage,
            collector=(
                UsageCollector(store=usage, settings=settings.usage)
                if settings.usage.enabled
                else None
            ),
            budget=budget,
            wake_fifo=wake_path(settings.database.path),
            notifier=notices,
            preview=PreviewServer(
                settings.projects.root,
                host=settings.projects.preview_host,
                port=settings.projects.preview_port,
            ),
            browser=find_browser(settings.projects.browser),
        )
    finally:
        await detector.aclose()
        if telegram is not None:
            await telegram.aclose()
        await usage.close()
        await store.close()


def wake_path(db_path: Path) -> Path:
    """The FIFO next to the event log: the CLI and batond find it through the same setting."""
    return db_path.with_name(db_path.name + ".wake")


def poke(db_path: Path) -> bool:
    """Wake a running batond now, so it does not wait out its idle sleep.

    Writes one byte to the FIFO without blocking. False when no batond listens (no FIFO,
    or nothing reading it); the change is picked up on batond's next cycle anyway.
    """
    try:
        fd = os.open(wake_path(db_path), os.O_WRONLY | os.O_NONBLOCK)
    except OSError:  # FileNotFoundError, or ENXIO: no reader
        return False
    try:
        os.write(fd, b"!")
    except BlockingIOError:
        pass  # the pipe is full: a wake-up is already pending
    finally:
        os.close(fd)
    return True


@contextmanager
def listening(fifo: Path, wake: asyncio.Event) -> Iterator[None]:
    """Set ``wake`` whenever something is written to ``fifo``, while in the block.

    The FIFO is created owner-only. It is opened read-write, so it never reads as
    closed when a writer goes away. A path that exists and is not a FIFO is left alone,
    and batond falls back to its idle sleep.
    """
    log = get_logger("baton.daemon")
    try:
        fifo.parent.mkdir(parents=True, exist_ok=True)
        with suppress(FileExistsError):
            os.mkfifo(fifo, 0o600)
        if not stat.S_ISFIFO(fifo.stat().st_mode):
            log.warning(
                "wake path is not a fifo; CLI commands wait for the next cycle", path=str(fifo)
            )
            yield
            return
        fd = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
    except OSError as err:
        log.warning("cannot listen for wake-ups", path=str(fifo), error=repr(err))
        yield
        return
    loop = asyncio.get_running_loop()

    def drain() -> None:
        with suppress(BlockingIOError):
            while os.read(fd, 512):
                pass
        wake.set()

    loop.add_reader(fd, drain)
    try:
        yield
    finally:
        loop.remove_reader(fd)
        os.close(fd)


async def add_task(
    store: EventStore,
    *,
    title: str,
    instructions: str,
    workdir: str,
    clock: Clock | None = None,
) -> TaskId:
    task_id = TaskId(f"t-{uuid.uuid4().hex[:8]}")
    now = (clock or SystemClock()).now()
    await store.append(
        [
            TaskCreated(
                occurred_at=now,
                task_id=task_id,
                title=title,
                instructions=instructions,
                workdir=workdir,
            )
        ]
    )
    return task_id


async def run_pending(
    runtime: Runtime, *, seen: dict[TaskId, Signature] | None = None
) -> dict[TaskId, TaskStatus]:
    """Run, once and oldest first, every task that may make progress now, side by side as
    the slots allow (ADR 0017); returns once every one of them has stopped.

    ``seen`` remembers, across calls, the state each task was last run in, so a
    task that is waiting for the same thing is not run (and reported) again.
    """
    seen = {} if seen is None else seen
    dispatcher = _dispatcher(runtime, seen)
    results: dict[TaskId, TaskStatus] = {}
    ran: set[TaskId] = set()
    try:
        while True:
            # A task that waited for a slot, or for the task before it, may start now.
            ran.update(await dispatcher.start_ready(skip=ran))
            if not dispatcher.running:
                break
            await dispatcher.wait_any()
            results.update(dispatcher.finished())
    finally:
        await dispatcher.aclose()
    results.update(dispatcher.finished())
    return results


def _dispatcher(runtime: Runtime, seen: dict[TaskId, Signature]) -> Dispatcher:
    return Dispatcher(
        runner=runtime.runner,
        store=runtime.store,
        settings=runtime.settings,
        clock=runtime.clock,
        seen=seen,
    )


async def serve(
    runtime: Runtime,
    *,
    stop: asyncio.Event,
    idle_s: float = 30,
    max_cycles: int | None = None,
) -> None:
    """Run tasks until ``stop`` is set: the batond main loop.

    Between cycles it sleeps until new work may exist: ``idle_s`` to notice new
    tasks, or less when an agent's limit resets sooner.
    """
    seen: dict[TaskId, Signature] = {}
    bot, operator = runtime.bot, runtime.operator
    bot_task = (
        asyncio.create_task(keep_running(lambda: run_bot(bot, operator)))
        if bot is not None and operator is not None
        else None
    )
    if bot_task is not None:
        bot_task.add_done_callback(_bot_stopped)
    collecting = (
        asyncio.create_task(runtime.collector.run(stop)) if runtime.collector is not None else None
    )
    listen = listening(runtime.wake_fifo, runtime.wake) if runtime.wake_fifo else nullcontext()
    # Only batond serves the preview; the CLI opens a runtime too, and must not take the port.
    if runtime.preview is not None:
        runtime.preview.start()
    try:
        with listen:
            # Stopping does not wait for a turn to end, which can take hours: the run in
            # progress is cancelled, its attempt stays active in the log, and the next start
            # re-attaches to it. A write cut short is rolled back whole.
            looping = asyncio.create_task(
                _serve_loop(runtime, stop=stop, idle_s=idle_s, max_cycles=max_cycles, seen=seen)
            )
            stopping = asyncio.create_task(stop.wait())
            try:
                await asyncio.wait({looping, stopping}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                stopping.cancel()
                if not looping.done():
                    looping.cancel()
                    with suppress(asyncio.CancelledError):
                        await looping
            if not looping.cancelled():
                looping.result()  # a fault of the loop itself still surfaces
    finally:
        if runtime.preview is not None:
            runtime.preview.stop()
        if bot_task is not None:
            bot_task.cancel()
            # A bot that already failed must not fail batond's shutdown with its error.
            await asyncio.gather(bot_task, return_exceptions=True)
        if collecting is not None:
            # It stops its baton-detect when stop is set; cancel if the loop ended otherwise.
            collecting.cancel()
            with suppress(asyncio.CancelledError):
                await collecting


def _bot_stopped(task: asyncio.Task[None]) -> None:
    # Notices still go out through the notifier; only remote actions are gone.
    if not task.cancelled() and (err := task.exception()) is not None:
        get_logger("baton.daemon").error("telegram bot stopped", error=repr(err))


async def _serve_loop(
    runtime: Runtime,
    *,
    stop: asyncio.Event,
    idle_s: float,
    max_cycles: int | None,
    seen: dict[TaskId, Signature],
) -> None:
    cycles = 0
    log = get_logger("baton.daemon")
    dispatcher = _dispatcher(runtime, seen)
    try:
        while not stop.is_set():
            if runtime.preview is not None:
                runtime.preview.start()  # again each cycle until its address can be used
            results = dispatcher.finished()
            try:
                await advance_projects(runtime)
            except Exception:  # the next cycle looks again
                log.exception("advancing projects failed")
            await dispatcher.start_ready()
            cycles += 1
            if max_cycles is not None and cycles >= max_cycles:
                await dispatcher.wait_all()  # the cycle's tasks run to their end
                break
            if TaskStatus.COMPLETED in results.values():
                continue  # a project's next step may start now, not after the idle time
            availability = fold_availability(
                await runtime.store.read(), cooldown=runtime.settings.limit_cooldown
            )
            now = runtime.clock.now()
            delay = idle_s
            if (wake_at := next_wake(availability, runtime.settings.agents, now)) is not None:
                delay = max(0.0, min(delay, (wake_at - now).total_seconds()))
            log.debug("sleeping", seconds=delay, running=len(dispatcher.running))
            # A worker that ends wakes the loop too: its slot can be filled at once.
            await _sleep(delay, stop, runtime.wake, dispatcher.changed)
            runtime.wake.clear()
            dispatcher.changed.clear()
    finally:
        await dispatcher.aclose()


async def advance_projects(runtime: Runtime) -> None:
    """Propose the plans of finished plan tasks, and deliver finished projects (ADR 0016)."""
    proposed = await propose_plans(runtime.store, clock=runtime.clock, read_text=_read_plan)
    halted = await halt_stopped_projects(runtime.store, clock=runtime.clock)
    delivered = await deliver_projects(
        runtime.store,
        clock=runtime.clock,
        workspace=GitWorkspace(),
        show=Showcase(runtime.preview, runtime.browser),
    )
    for notice in [*proposed, *halted, *delivered]:
        if runtime.notifier is not None:
            await runtime.notifier.notify(notice)


def _read_plan(path: Path) -> str:
    # A plan is a few kilobytes; a huge file is not one.
    with path.open(encoding="utf-8") as file:
        return file.read(256_000)


async def _sleep(delay: float, *events: asyncio.Event) -> None:
    """Sleep for ``delay`` seconds or until one of ``events`` is set."""
    waiters = [asyncio.create_task(event.wait()) for event in events]
    try:
        await asyncio.wait(waiters, timeout=delay, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for waiter in waiters:
            waiter.cancel()
