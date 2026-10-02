"""Roadmap slice 0, end to end with fakes.

A simulated Claude hits its usage limit in the middle of a task; coban notices,
hands the task to a simulated Codex, which finishes; the operator is notified.
See tests/support/simulated_agents.py for how the agents are simulated.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from coban.adapters import ADAPTERS
from coban.budget.availability import fold_availability
from coban.core.events import (
    AgentStateObserved,
    AttemptEnded,
    AttemptInterrupted,
    AttemptLocated,
    AttemptStarted,
    TaskCompleted,
    TaskCreated,
)
from coban.core.fakes import FixedClock, InMemoryEventStore, RecordingNotifier
from coban.core.model import (
    AgentKind,
    AgentState,
    AttemptOutcome,
    InterruptReason,
    TaskId,
    TaskStatus,
)
from coban.core.notify import NoticeKind
from coban.core.projection import project
from coban.scheduler.runner import HANDOFF_NOTE, RunnerSettings, TaskRunner

from simulated_agents import SimulatedAgents

NOW = datetime(2026, 10, 2, 9, 0, tzinfo=UTC)
TASK = TaskId("power")
INSTRUCTIONS = "Add a power(a, b) function to calc.py with a test."

SETTINGS = RunnerSettings(
    agents=(AgentKind.CLAUDE, AgentKind.CODEX),
    start_timeout_s=2,
    turn_timeout_s=2,
    poll_interval_s=0.05,
    timezone="UTC",
)


async def setup(
    *, crash_after_prompt: bool = False, codex_update_prompt: bool = False
) -> tuple[InMemoryEventStore, SimulatedAgents, RecordingNotifier, TaskRunner]:
    clock = FixedClock(NOW)
    store = InMemoryEventStore()
    await store.append(
        [
            TaskCreated(
                occurred_at=NOW,
                task_id=TASK,
                title="power function",
                instructions=INSTRUCTIONS,
                workdir="/work/calc",
            )
        ]
    )
    host = SimulatedAgents(
        clock, crash_after_prompt=crash_after_prompt, codex_update_prompt=codex_update_prompt
    )
    notifier = RecordingNotifier()
    runner = TaskRunner(
        store=store, host=host, adapters=ADAPTERS, notifier=notifier, clock=clock, settings=SETTINGS
    )
    return store, host, notifier, runner


async def test_a_limited_claude_hands_the_task_to_codex_which_finishes_it() -> None:
    store, host, notifier, runner = await setup()

    status = await runner.run(TASK)

    assert status is TaskStatus.COMPLETED
    events = [s.event for s in await store.read()]
    kinds = [type(e).__name__ for e in events if not isinstance(e, AgentStateObserved)]
    assert kinds == [
        "TaskCreated",
        "AttemptStarted",  # claude
        "AttemptLocated",  # pane
        "AttemptLocated",  # claude's session id, learned from the pane
        "AttemptInterrupted",
        "AttemptEnded",
        "AttemptStarted",  # codex
        "AttemptLocated",
        "AttemptLocated",
        "AttemptEnded",
        "TaskCompleted",
    ]
    starts = [e for e in events if isinstance(e, AttemptStarted)]
    assert [e.agent for e in starts] == [AgentKind.CLAUDE, AgentKind.CODEX]

    limit = next(e for e in events if isinstance(e, AttemptInterrupted))
    assert limit.reason is InterruptReason.RATE_LIMITED
    # The fake prints "resets <now + 30 min>"; the adapter reads it back.
    assert limit.resume_not_before == NOW + timedelta(minutes=30)
    ended = [e for e in events if isinstance(e, AttemptEnded)]
    assert [e.outcome for e in ended] == [AttemptOutcome.ABANDONED, AttemptOutcome.SUCCEEDED]
    sessions = [e.session_ref for e in events if isinstance(e, AttemptLocated) and e.session_ref]
    assert sessions == ["claude-session", "codex-session"]

    observed = [(e.state, e.evidence) for e in events if isinstance(e, AgentStateObserved)]
    assert (AgentState.RATE_LIMITED, "coban:claude_usage_limit") in observed
    assert (AgentState.IDLE, "coban:codex_idle_prompt") in observed

    assert host.prompts == [
        (AgentKind.CLAUDE, INSTRUCTIONS),
        (AgentKind.CODEX, f"{HANDOFF_NOTE}\n\n{INSTRUCTIONS}"),
    ]
    assert notifier.kinds == [
        NoticeKind.TASK_STARTED,
        NoticeKind.AGENT_LIMITED,
        NoticeKind.TASK_HANDED_OFF,
        NoticeKind.TASK_COMPLETED,
    ]
    assert "Available again at 2026-10-02 09:30 UTC" in notifier.notices[1].text

    board = project(await store.read())
    assert board.tasks[TASK].status is TaskStatus.COMPLETED
    availability = fold_availability(await store.read())
    assert availability.available([AgentKind.CLAUDE, AgentKind.CODEX], NOW) == [AgentKind.CODEX]
    assert any(isinstance(e, TaskCompleted) for e in events)


async def test_with_every_agent_limited_the_task_waits() -> None:
    store, host, notifier, _ = await setup()
    only_claude = TaskRunner(
        store=store,
        host=host,
        adapters=ADAPTERS,
        notifier=notifier,
        clock=FixedClock(NOW),
        settings=replace(SETTINGS, agents=(AgentKind.CLAUDE,)),
    )

    status = await only_claude.run(TASK)

    assert status is TaskStatus.PENDING
    assert notifier.kinds == [
        NoticeKind.TASK_STARTED,
        NoticeKind.AGENT_LIMITED,
        NoticeKind.WAITING_FOR_AGENT,
    ]


async def test_an_agent_that_crashes_stops_the_task_for_a_person() -> None:
    store, _host, notifier, runner = await setup(crash_after_prompt=True)

    status = await runner.run(TASK)

    assert status is TaskStatus.WAITING
    interrupted = [s.event for s in await store.read() if isinstance(s.event, AttemptInterrupted)]
    assert [e.reason for e in interrupted] == [InterruptReason.CRASHED]
    assert notifier.kinds == [NoticeKind.TASK_STARTED, NoticeKind.TASK_STOPPED]


async def test_a_terminal_task_is_left_alone() -> None:
    store, host, notifier, runner = await setup()
    assert await runner.run(TASK) is TaskStatus.COMPLETED
    before = len(await store.read())

    assert await runner.run(TASK) is TaskStatus.COMPLETED
    assert len(await store.read()) == before
    assert len(host.prompts) == 2
    assert notifier.kinds[-1] is NoticeKind.TASK_COMPLETED


async def test_codex_update_prompt_is_skipped_on_the_way_to_the_task() -> None:
    _store, host, notifier, runner = await setup(codex_update_prompt=True)

    assert await runner.run(TASK) is TaskStatus.COMPLETED

    assert host.dialog_answers == [("Down", "Enter")]  # "2. Skip"
    assert NoticeKind.NEEDS_HUMAN not in notifier.kinds
