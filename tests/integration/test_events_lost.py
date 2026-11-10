"""Reconciliation when herdr's subscription loses events (ROADMAP step 6), under load.

herdr's change events are invalidation signals: after ``events_lost`` the pane host
subscribes again and reads the pane, and that read is authoritative
(:meth:`HerdrPaneHost.watch`). :class:`LossyAgents` gives the runner a watch that behaves
like that one when herdr lags: events go missing, a subscription is dropped and opened
again while the agent goes on changing its screen, a burst repeats one state, and the
connection ends in the middle of a turn. The periodic read between events is set far
beyond the turn timeout, so only the watch can tell the runner what the screen shows.

Every attempt has to end as its screen shows, and the log has to hold each attempt's
events once: one start, one prompt, one end, no state recorded twice in a row.
"""

from __future__ import annotations

import asyncio
import random
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from baton_herdr.adapters import ADAPTERS
from baton_herdr.core.events import (
    AgentStateObserved,
    AttemptEnded,
    AttemptInterrupted,
    AttemptPrompted,
    AttemptStarted,
    TaskCompleted,
    TaskCreated,
)
from baton_herdr.core.fakes import FakePaneHost, FixedClock, InMemoryEventStore, RecordingNotifier
from baton_herdr.core.model import (
    AgentKind,
    AgentState,
    AttemptOutcome,
    InterruptReason,
    TaskId,
    TaskStatus,
)
from baton_herdr.core.notify import NoticeKind
from baton_herdr.core.panes import PaneHostUnavailableError
from baton_herdr.core.projection import Board, project
from baton_herdr.scheduler.dispatch import Dispatcher
from baton_herdr.scheduler.runner import RunnerSettings, TaskRunner

from detector_binary import rust_detector
from simulated_agents import SimulatedAgents

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Sequence

    from baton_herdr.core.events import Event, StoredEvent
    from baton_herdr.core.panes import PaneObservation

NOW = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)
SETTINGS = RunnerSettings(
    agents=(AgentKind.CLAUDE, AgentKind.CODEX),
    start_timeout_s=10,
    turn_timeout_s=10,
    # Far beyond the timeouts: a change the watch does not deliver is never read.
    poll_interval_s=600,
    timezone="UTC",
    default_agent_slots=64,
    max_parallel_tasks=64,
)


class LossyAgents(SimulatedAgents):
    """Simulated agents whose watch loses events the way herdr's subscription does.

    Each change is, at random: delivered (and in a burst, repeated); lost, after which
    the subscription is opened again ``gap_s`` later and the pane read, as
    :meth:`HerdrPaneHost.watch` does after ``events_lost``; or the end of the connection
    (:class:`PaneHostUnavailableError`). ``lose_final`` loses the change that ends a turn
    (a finished turn, or Claude's limit screen) every time, in the way ``final_loss`` says.
    """

    def __init__(  # noqa: PLR0913 - one knob per way of losing events
        self,
        clock: FixedClock,
        *,
        seed: int = 0,
        lost: float = 0.0,
        dropped: float = 0.0,
        burst: float = 0.0,
        gap_s: float = 0.02,
        lose_final: bool = False,
        final_loss: str = "lost",
    ) -> None:
        super().__init__(clock, session_per_pane=True)
        self._rng = random.Random(seed)  # noqa: S311 - reproducible losses, not secrets
        self._lost, self._dropped, self._burst = lost, dropped, burst
        self._gap_s = gap_s
        self._lose_final = lose_final
        self._final_loss = final_loss
        self.subscriptions = 0
        self.losses = 0
        self.drops = 0

    def _ends_turn(self, pane_id: str) -> bool:
        running = self._running.get(pane_id)
        return running is not None and running[1] == 2  # the scripts' last step

    def _loss(self, pane_id: str) -> str:
        if self._lose_final and self._ends_turn(pane_id):
            return self._final_loss
        roll = self._rng.random()
        if roll < self._lost:
            return "lost"
        if roll < self._lost + self._dropped:
            return "dropped"
        if roll < self._lost + self._dropped + self._burst:
            return "burst"
        return "delivered"

    async def watch(self, pane_id: str) -> AsyncGenerator[PaneObservation]:
        while True:  # one pass per subscription
            self.subscriptions += 1
            changes = FakePaneHost.watch(self, pane_id)
            try:
                await anext(changes)  # subscribed; then the authoritative read
                yield await self.observe(pane_id)
                async for _change in changes:
                    loss = self._loss(pane_id)
                    if loss == "lost":
                        self.losses += 1
                        # Changes made while baton resubscribes reach it only by the read.
                        await asyncio.sleep(self._gap_s)
                        break
                    if loss == "dropped":
                        self.drops += 1
                        msg = "herdr closed the connection"
                        raise PaneHostUnavailableError(msg)
                    for _ in range(3 if loss == "burst" else 1):
                        yield await self.observe(pane_id)
            finally:
                await changes.aclose()


def created(task_id: TaskId) -> TaskCreated:
    return TaskCreated(
        occurred_at=NOW, task_id=task_id, title=task_id, instructions="x", workdir="/work/calc"
    )


async def run_tasks(
    host: LossyAgents,
    clock: FixedClock,
    count: int,
    *,
    settings: RunnerSettings = SETTINGS,
    through_batond: bool = False,
) -> tuple[list[TaskId], InMemoryEventStore, RecordingNotifier, list[TaskStatus]]:
    """Run ``count`` tasks side by side to their end: each with its own ``run``, or, with
    ``through_batond``, as batond's loop does, which runs a failed worker's task again."""
    store = InMemoryEventStore()
    tasks = [TaskId(f"t{n:02}") for n in range(count)]
    await store.append([created(task_id) for task_id in tasks])
    notifier = RecordingNotifier()
    runner = TaskRunner(
        detector=rust_detector(),
        store=store,
        host=host,
        adapters=ADAPTERS,
        notifier=notifier,
        clock=clock,
        settings=settings,
    )
    async with asyncio.timeout(60):
        if through_batond:
            await run_like_batond(runner, store, clock, settings, tasks)
        else:
            async with asyncio.TaskGroup() as group:
                for task_id in tasks:
                    group.create_task(runner.run(task_id))
    board = project(await store.read())
    return tasks, store, notifier, [board.tasks[t].status for t in tasks]


async def run_like_batond(
    runner: TaskRunner,
    store: InMemoryEventStore,
    clock: FixedClock,
    settings: RunnerSettings,
    tasks: Sequence[TaskId],
) -> None:
    """batond's loop (``daemon._serve_loop``): start what may start, wait for a worker."""
    dispatcher = Dispatcher(runner=runner, store=store, settings=settings, clock=clock, seen={})
    try:
        while not _all_over(project(await store.read()), tasks):
            dispatcher.finished()
            await dispatcher.start_ready()
            await dispatcher.wait_any()
            await asyncio.sleep(0)
    finally:
        await dispatcher.aclose()


def _all_over(board: Board, tasks: Sequence[TaskId]) -> bool:
    return all(board.tasks[t].status.is_terminal for t in tasks)


def attempts(events: Sequence[StoredEvent]) -> int:
    return sum(isinstance(s.event, AttemptStarted) for s in events)


def assert_log_holds_each_attempt_once(events: Sequence[Event]) -> None:
    """One start, one prompt and one end per attempt; one completion per task; no
    state recorded twice in a row for an attempt."""
    starts = Counter(e.attempt_id for e in events if isinstance(e, AttemptStarted))
    prompts = Counter(e.attempt_id for e in events if isinstance(e, AttemptPrompted))
    ends = Counter(e.attempt_id for e in events if isinstance(e, AttemptEnded))
    interrupts = Counter(e.attempt_id for e in events if isinstance(e, AttemptInterrupted))
    completions = Counter(e.task_id for e in events if isinstance(e, TaskCompleted))
    assert set(starts.values()) == {1}
    assert prompts == starts  # every attempt was prompted once: no nudge, no second send
    assert ends == starts
    assert set(interrupts.values()) <= {1}
    assert set(completions.values()) == {1}
    last: dict[object, AgentState] = {}
    for e in events:
        if isinstance(e, AgentStateObserved):
            assert last.get(e.attempt_id) is not e.state, f"{e.state} twice for {e.attempt_id}"
            last[e.attempt_id] = e.state


def assert_attempts_end_as_their_screens_show(events: Sequence[Event]) -> None:
    """A limit screen interrupts the attempt as limited; a finished turn ends it done."""
    agents = {e.attempt_id: e.agent for e in events if isinstance(e, AttemptStarted)}
    interrupted = {e.attempt_id: e.reason for e in events if isinstance(e, AttemptInterrupted)}
    ended = {e.attempt_id: e.outcome for e in events if isinstance(e, AttemptEnded)}
    seen: dict[object, AgentState] = {}
    for e in events:
        if isinstance(e, AgentStateObserved):
            seen[e.attempt_id] = e.state
    for attempt_id, agent in agents.items():
        if agent is AgentKind.CLAUDE:  # claude-limit.toml: the limit screen ends the turn
            assert seen[attempt_id] is AgentState.RATE_LIMITED
            assert interrupted[attempt_id] is InterruptReason.RATE_LIMITED
            assert ended[attempt_id] is AttemptOutcome.ABANDONED
        else:  # codex-finish.toml: done, with its end mark
            assert seen[attempt_id] is AgentState.IDLE
            assert attempt_id not in interrupted
            assert ended[attempt_id] is AttemptOutcome.SUCCEEDED


async def assert_every_task_finished(
    tasks: Sequence[TaskId],
    store: InMemoryEventStore,
    notifier: RecordingNotifier,
    statuses: Sequence[TaskStatus],
) -> None:
    assert statuses == [TaskStatus.COMPLETED] * len(tasks)
    events = [s.event for s in await store.read()]
    assert_log_holds_each_attempt_once(events)
    assert_attempts_end_as_their_screens_show(events)
    board = project(await store.read())
    assert all(board.tasks[t].status is TaskStatus.COMPLETED for t in tasks)
    assert Counter(notifier.kinds)[NoticeKind.TASK_COMPLETED] == len(tasks)


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(NOW)


async def test_lost_events_on_many_panes_still_end_every_attempt_as_its_screen_shows(
    clock: FixedClock,
) -> None:
    host = LossyAgents(clock, seed=6, lost=0.4, burst=0.3)

    tasks, store, notifier, statuses = await run_tasks(host, clock, 16)

    await assert_every_task_finished(tasks, store, notifier, statuses)
    assert host.losses > 0
    assert host.subscriptions > host.losses  # every loss was followed by a new subscription


async def test_a_limit_screen_or_a_finished_turn_can_appear_while_events_are_lost(
    clock: FixedClock,
) -> None:
    """On every pane, the change that ends the turn falls in the gap."""
    host = LossyAgents(clock, lose_final=True, gap_s=0.1)

    tasks, store, notifier, statuses = await run_tasks(host, clock, 8)

    await assert_every_task_finished(tasks, store, notifier, statuses)
    # Every attempt's last change: Claude's limit screen, Codex's finished turn.
    assert host.losses == attempts(await store.read())


@pytest.mark.parametrize("seed", range(5))
async def test_random_losses_and_bursts_never_duplicate_or_drop_log_events(
    clock: FixedClock, seed: int
) -> None:
    host = LossyAgents(clock, seed=seed, lost=0.35, burst=0.35, gap_s=0.03)

    tasks, store, notifier, statuses = await run_tasks(
        host, clock, 10, settings=replace(SETTINGS, close_finished_panes=seed % 2 == 0)
    )

    await assert_every_task_finished(tasks, store, notifier, statuses)


# A watch that ends with PaneHostUnavailableError fails the task's run (herdr's own watch
# reports that only when it cannot subscribe again). batond runs the task again, which
# re-attaches to its attempt: the log must not show the seam.


async def test_a_watch_given_up_in_the_middle_of_a_turn_is_picked_up_again_by_batond(
    clock: FixedClock,
) -> None:
    host = LossyAgents(clock, lose_final=True, final_loss="dropped")

    tasks, store, notifier, statuses = await run_tasks(host, clock, 8, through_batond=True)

    await assert_every_task_finished(tasks, store, notifier, statuses)
    assert host.drops == attempts(await store.read())


@pytest.mark.parametrize("seed", range(3))
async def test_watches_given_up_at_random_never_duplicate_or_drop_log_events(
    clock: FixedClock, seed: int
) -> None:
    host = LossyAgents(clock, seed=seed, dropped=0.25, lost=0.25, burst=0.25)

    tasks, store, notifier, statuses = await run_tasks(host, clock, 12, through_batond=True)

    await assert_every_task_finished(tasks, store, notifier, statuses)
    assert host.drops > 0
