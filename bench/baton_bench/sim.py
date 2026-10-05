"""Run one simulated world through batond's own loop and measure what happened."""

from __future__ import annotations

import asyncio
import os
import random
import statistics
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from baton_bench.vtime import VirtualClock, VirtualTimeLoop
from baton_bench.world import WINDOW, Quota, World
from baton_herdr.adapters import ADAPTERS
from baton_herdr.budget.load import BudgetReader
from baton_herdr.core.config import BudgetSettings, SchedulerSettings
from baton_herdr.core.detection import DetectionRequest, DetectionResult
from baton_herdr.core.events import AttemptInterrupted, AttemptPrompted, TaskCreated
from baton_herdr.core.fakes import InMemoryEventStore, InMemoryUsageStore, RecordingNotifier
from baton_herdr.core.model import AgentKind, TaskId, TaskStatus
from baton_herdr.core.notify import NoticeKind
from baton_herdr.core.projection import project
from baton_herdr.core.usage import RateLimitObservation, RateLimitWindow, UsageRecord, UsageTokens
from baton_herdr.daemon import Runtime, runner_settings, serve
from baton_herdr.scheduler.runner import TaskRunner

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from baton_bench.world import Workdir
    from baton_herdr.core.events import Event, StoredEvent

START = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)
REPO = Path(__file__).parents[2]
# A world that has not settled by then is cut off; its open tasks count as unfinished.
HORIZON = timedelta(days=2)


@dataclass(frozen=True, slots=True)
class Budget:
    """How baton learns budgets in a world: the [usage] and [budget] settings."""

    collect_usage: bool = True  # [usage] enabled, the default
    configured_window: bool = False  # [budget] claude_window_tokens set to the true cap
    reserve_percent: float = 10


@dataclass(slots=True)
class Setup:
    """One world: its tasks, its agents' quotas, and what they used before it starts."""

    tasks: list[Workdir]
    quotas: dict[AgentKind, Quota]
    budget: Budget = field(default_factory=Budget)


@dataclass(frozen=True, slots=True)
class TaskResult:
    status: TaskStatus
    needs_person: bool
    steps: int
    redone: int
    tokens: int
    wasted: int
    overhead: int
    attempts: int


@dataclass(frozen=True, slots=True)
class WorldResult:
    tasks: tuple[TaskResult, ...]
    # Seconds from an agent stopping (limit, crash, hang) to baton recording it.
    detection: tuple[tuple[str, float], ...]
    # Seconds from an agent stopping to the next prompt that carries the task on.
    recovery: tuple[tuple[str, float], ...]
    # Seconds from the first agent becoming free again to the task going on, after a wait.
    after_reset: tuple[float, ...]
    # Every interruption baton recorded: its reason and its detail.
    interruptions: tuple[tuple[str, str | None], ...]
    limits: int
    makespan_s: float


def detector_path() -> str:
    """baton-detect: BATON_DETECT_BIN, else the release or debug build here, else PATH."""
    if explicit := os.environ.get("BATON_DETECT_BIN"):
        return explicit
    for build in ("release", "debug"):
        if (path := REPO / "target" / build / "baton-detect").is_file():
            return str(path)
    return "baton-detect"


class BlockingDetector:
    """``baton-detect classify``, asked with blocking pipe I/O.

    batond's ``ProcessDetector`` waits for answers on the event loop. On simulated time,
    the loop would jump ahead while the real process answers, and time the answer out.
    Asked here without the loop, an answer takes no simulated time at all.
    """

    def __init__(self, binary: str) -> None:
        self._process = subprocess.Popen(  # noqa: S603 - the classifier, fixed arguments
            [binary, "classify"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
        )

    async def classify(self, request: DetectionRequest) -> DetectionResult:
        stdin, stdout = self._process.stdin, self._process.stdout
        if stdin is None or stdout is None:  # pragma: no cover - opened with PIPE
            raise RuntimeError("no pipes to baton-detect")
        stdin.write(request.model_dump_json() + "\n")
        stdin.flush()
        line = stdout.readline()
        if not line:
            raise RuntimeError(f"baton-detect exited ({self._process.wait()})")
        return DetectionResult.model_validate_json(line)

    async def aclose(self) -> None:
        if self._process.stdin is not None:
            self._process.stdin.close()
        self._process.wait()
        if self._process.stdout is not None:
            self._process.stdout.close()


def simulate(build: Callable[[random.Random], Setup], seed: str) -> WorldResult:
    """Build the world from ``seed`` and run it to the end, on simulated time."""
    rng = random.Random(seed)
    setup = build(rng)
    return asyncio.run(_run(setup, rng), loop_factory=VirtualTimeLoop)


async def _run(setup: Setup, rng: random.Random) -> WorldResult:
    clock = VirtualClock(START)
    store = InMemoryEventStore()
    usage = InMemoryUsageStore() if setup.budget.collect_usage else None
    workdirs = {f"/work/t-{n:03d}": task for n, task in enumerate(setup.tasks, start=1)}
    world = World(clock=clock, rng=rng, quotas=setup.quotas, workdirs=workdirs, usage=usage)
    if usage is not None:
        await _usage_so_far(usage, setup.quotas)
    scheduler = SchedulerSettings()
    settings = runner_settings(
        scheduler,
        BudgetSettings(reserve_percent=setup.budget.reserve_percent),
        collecting_usage=usage is not None,
    )
    claude = setup.quotas.get(AgentKind.CLAUDE)
    budget = BudgetReader(
        usage=usage,
        cooldown=settings.limit_cooldown,
        claude_window_tokens=claude.cap if setup.budget.configured_window and claude else None,
    )
    notifier = RecordingNotifier()
    detector = BlockingDetector(detector_path())
    runner = TaskRunner(
        detector=detector,
        store=store,
        host=world,
        adapters=ADAPTERS,
        notifier=notifier,
        clock=clock,
        settings=settings,
        budget=budget,
    )
    runtime = Runtime(
        store=store, runner=runner, settings=settings, clock=clock, host=world, budget=budget
    )
    await store.append(
        [
            TaskCreated(
                occurred_at=START,
                task_id=TaskId(workdir.removeprefix("/work/")),
                title=f"task {n}",
                instructions="Carry out the steps.",
                workdir=workdir,
            )
            for n, workdir in enumerate(workdirs, start=1)
        ]
    )
    stop = asyncio.Event()
    try:
        await asyncio.gather(
            serve(runtime, stop=stop), _until_settled(store, notifier, clock, stop)
        )
    finally:
        await detector.aclose()
    return _measure(await store.read(), notifier, world, clock.now())


async def _usage_so_far(usage: InMemoryUsageStore, quotas: dict[AgentKind, Quota]) -> None:
    """What the collector would have recorded before the world starts."""
    for agent, quota in quotas.items():
        if quota.start is None or quota.used == 0:
            continue
        tokens = UsageTokens(input=quota.used, output=0, cache_read=0, cache_write=0)
        events: list[UsageRecord | RateLimitObservation] = [
            UsageRecord(
                agent=agent.value,  # type: ignore[arg-type]
                session_id=f"{agent.value}-before",
                record_id=f"{agent.value}-before",
                at=quota.start,
                tokens=tokens,
            )
        ]
        if agent is AgentKind.CODEX:
            events.append(
                RateLimitObservation(
                    agent="codex",
                    session_id="codex-before",
                    at=quota.start,
                    windows=(
                        RateLimitWindow(
                            name="primary",
                            window_minutes=300,
                            used_percent=100.0 * quota.used / quota.cap,
                            resets_at=quota.start + WINDOW,
                        ),
                    ),
                )
            )
        await usage.record(events)


async def _until_settled(
    store: InMemoryEventStore, notifier: RecordingNotifier, clock: VirtualClock, stop: asyncio.Event
) -> None:
    """Stop batond once every task is done or waits for a person, or at the horizon."""
    while clock.now() < START + HORIZON:
        await asyncio.sleep(60)
        board = project(await store.read())
        open_tasks = [t for t in board.tasks.values() if not t.status.is_terminal]
        if all(_needs_person(t.task_id, t.status, notifier) for t in open_tasks):
            break
    stop.set()


def _needs_person(task_id: TaskId, status: TaskStatus, notifier: RecordingNotifier) -> bool:
    if status is TaskStatus.NEEDS_HUMAN:
        return True
    last = [n for n in notifier.notices if n.task_id == task_id]
    return bool(last) and last[-1].kind is NoticeKind.NEEDS_HUMAN and status is TaskStatus.WAITING


def _measure(
    events: Sequence[StoredEvent], notifier: RecordingNotifier, world: World, end: datetime
) -> WorldResult:
    board = project(events)
    tasks = []
    for task in board.tasks.values():
        workdir = world.workdirs[task.workdir]
        tasks.append(
            TaskResult(
                status=task.status,
                needs_person=_needs_person(task.task_id, task.status, notifier),
                steps=workdir.steps,
                redone=workdir.redone,
                tokens=workdir.tokens,
                wasted=workdir.wasted,
                overhead=workdir.overhead,
                attempts=len(task.attempts),
            )
        )
    by_workdir = {task.workdir: task.task_id for task in board.tasks.values()}
    resets = sorted(i.resets_at for i in world.incidents if i.resets_at is not None)
    detection, recovery, after_reset = [], [], []
    for incident in world.incidents:
        task_id = by_workdir[incident.workdir]
        seen = _next(events, AttemptInterrupted, task_id, after=incident.at)
        if seen is None:
            continue
        noticed_at = seen.event.occurred_at
        detection.append((incident.kind, (noticed_at - incident.at).total_seconds()))
        carried_on = _next(events, AttemptPrompted, task_id, after_seq=seen.seq)
        if carried_on is None:
            continue
        went_on_at = carried_on.event.occurred_at
        recovery.append((incident.kind, (went_on_at - incident.at).total_seconds()))
        freed = [r for r in resets if noticed_at <= r <= went_on_at]
        if freed:
            after_reset.append((went_on_at - freed[0]).total_seconds())
    return WorldResult(
        tasks=tuple(tasks),
        detection=tuple(detection),
        recovery=tuple(recovery),
        after_reset=tuple(after_reset),
        interruptions=tuple(
            (e.event.reason.value, e.event.detail)
            for e in events
            if isinstance(e.event, AttemptInterrupted)
        ),
        limits=sum(1 for i in world.incidents if i.kind == "limit"),
        makespan_s=(_last_completion(events) or end - START).total_seconds(),
    )


def _next(
    events: Sequence[StoredEvent],
    kind: type[Event],
    task_id: TaskId,
    *,
    after: datetime | None = None,
    after_seq: int = 0,
) -> StoredEvent | None:
    """The task's first event of ``kind`` from ``after`` on, or past position ``after_seq``."""
    return next(
        (
            e
            for e in events
            if isinstance(e.event, kind)
            and e.event.task_id == task_id
            and e.seq > after_seq
            and (after is None or e.event.occurred_at >= after)
        ),
        None,
    )


def _last_completion(events: Sequence[StoredEvent]) -> timedelta | None:
    project_ = project(events)
    done = [t for t in project_.tasks.values() if t.status is TaskStatus.COMPLETED]
    if len(done) != len(project_.tasks):
        return None
    return max(e.event.occurred_at for e in events) - START


def percentile(values: Sequence[float], share: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return float("nan")
    return ordered[min(len(ordered) - 1, round(share * (len(ordered) - 1)))]


def median(values: Sequence[float]) -> float:
    return statistics.median(values) if values else float("nan")
