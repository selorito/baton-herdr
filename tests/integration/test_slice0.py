"""Roadmap slice 0, end to end with fakes.

A simulated Claude hits its usage limit in the middle of a task; coban notices,
hands the task to a simulated Codex, which finishes; the operator is notified.
The agents play the same scripts as tools/fake-agent, and the pane host reports
the states herdr reports for those screens (rule-based idle for Claude, the idle
fallback for Codex), so the adapters classify exactly what they would live.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

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
from coban.core.fakes import FakePaneHost, FixedClock, InMemoryEventStore, RecordingNotifier
from coban.core.model import (
    AgentKind,
    AgentState,
    AttemptOutcome,
    InterruptReason,
    TaskId,
    TaskStatus,
)
from coban.core.notify import NoticeKind
from coban.core.panes import PaneObservation
from coban.core.projection import project
from coban.scheduler.runner import HANDOFF_NOTE, RunnerSettings, TaskRunner

import fake_agent

if TYPE_CHECKING:
    from collections.abc import Sequence

SCRIPTS = Path(__file__).parents[2] / "tools" / "fake-agent" / "scripts"
NOW = datetime(2026, 10, 2, 9, 0, tzinfo=UTC)
TASK = TaskId("power")
INSTRUCTIONS = "Add a power(a, b) function to calc.py with a test."

# What herdr reports for each step of the scripts (see test_fake_agent_live.py).
HOST_STATES = {
    AgentKind.CLAUDE: [
        (AgentState.IDLE, "herdr:rule:live_prompt_box"),
        (AgentState.WORKING, "herdr:rule:live_turn_working"),
        (AgentState.IDLE, "herdr:rule:live_prompt_box"),
    ],
    AgentKind.CODEX: [
        (AgentState.UNKNOWN, "herdr:idle-fallback"),
        (AgentState.WORKING, "herdr:rule:screen_working_fallback"),
        (AgentState.UNKNOWN, "herdr:idle-fallback"),
    ],
}
SCRIPT_FILES = {AgentKind.CLAUDE: "claude-limit.toml", AgentKind.CODEX: "codex-finish.toml"}


class SimulatedAgents(FakePaneHost):
    """A pane host whose panes run scripted agents when their launch command is typed."""

    def __init__(self, clock: FixedClock, *, crash_after_prompt: bool = False) -> None:
        super().__init__()
        self._clock = clock
        self._crash_after_prompt = crash_after_prompt
        self._typed: dict[str, str] = {}
        self._running: dict[str, tuple[AgentKind, int]] = {}
        self.prompts: list[tuple[AgentKind, str]] = []
        self._tasks: list[asyncio.Task[None]] = []

    async def send_text(self, pane_id: str, text: str) -> None:
        await super().send_text(pane_id, text)
        self._typed[pane_id] = text

    async def send_keys(self, pane_id: str, keys: Sequence[str]) -> None:
        await super().send_keys(pane_id, keys)
        command = self._typed.pop(pane_id, "")
        agent = next(
            (a for a in AgentKind if ADAPTERS.get(a) and ADAPTERS[a].launch_command() == command),
            None,
        )
        if list(keys) == ["Enter"] and agent is not None:
            self._running[pane_id] = (agent, 0)
            self._show(pane_id, prompt="")

    async def send_prompt(self, pane_id: str, text: str) -> None:
        await super().send_prompt(pane_id, text)
        agent, _ = self._running[pane_id]
        self.prompts.append((agent, text))
        self._running[pane_id] = (agent, 1)
        self._show(pane_id, prompt=text)
        self._tasks.append(asyncio.create_task(self._finish_turn(pane_id, text)))

    async def _finish_turn(self, pane_id: str, prompt: str) -> None:
        await asyncio.sleep(0.05)
        agent, _ = self._running[pane_id]
        if self._crash_after_prompt:
            self.set_observation(
                PaneObservation(
                    pane_id=pane_id, agent=None, state=AgentState.UNKNOWN, evidence="herdr:no-agent"
                )
            )
            return
        self._running[pane_id] = (agent, 2)
        self._show(pane_id, prompt=prompt)

    def _show(self, pane_id: str, *, prompt: str) -> None:
        agent, step = self._running[pane_id]
        script = fake_agent.load_script(SCRIPTS / SCRIPT_FILES[agent])
        self.screens[pane_id] = fake_agent.render(
            script.steps[step].screen, now=self._clock.now(), prompt=prompt
        )
        state, evidence = HOST_STATES[agent][step]
        self.set_observation(
            PaneObservation(
                pane_id=pane_id,
                agent=agent,
                state=state,
                evidence=evidence,
                session_ref=f"{agent.value}-session",
                cwd="/work/calc",
            )
        )


SETTINGS = RunnerSettings(
    agents=(AgentKind.CLAUDE, AgentKind.CODEX),
    start_timeout_s=2,
    turn_timeout_s=2,
    poll_interval_s=0.05,
    timezone="UTC",
)


async def setup(
    *, crash_after_prompt: bool = False
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
    host = SimulatedAgents(clock, crash_after_prompt=crash_after_prompt)
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
