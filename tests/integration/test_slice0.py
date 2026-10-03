"""Roadmap slice 0, end to end with fakes.

A simulated Claude hits its usage limit in the middle of a task; baton notices,
hands the task to a simulated Codex, which finishes; the operator is notified.
See tests/support/simulated_agents.py for how the agents are simulated.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from baton_herdr.adapters import ADAPTERS
from baton_herdr.budget.availability import fold_availability
from baton_herdr.core.contract import END_CONTRACT
from baton_herdr.core.events import (
    AgentChosen,
    AgentStateObserved,
    AttemptEnded,
    AttemptInterrupted,
    AttemptLocated,
    AttemptPrompted,
    AttemptStarted,
    TaskCompleted,
    TaskCreated,
)
from baton_herdr.core.fakes import FixedClock, InMemoryEventStore, RecordingNotifier
from baton_herdr.core.model import (
    AgentKind,
    AgentState,
    AttemptOutcome,
    InterruptReason,
    TaskId,
    TaskStatus,
)
from baton_herdr.core.notify import NoticeKind
from baton_herdr.core.projection import project
from baton_herdr.scheduler.runner import CONTINUE_NOTE, HANDOFF_NOTE, RunnerSettings, TaskRunner

from simulated_agents import SimulatedAgents

NOW = datetime(2026, 10, 2, 9, 0, tzinfo=UTC)
TASK = TaskId("power")
INSTRUCTIONS = "Add a power(a, b) function to calc.py with a test."


def sent(body: str) -> str:
    """A prompt as baton sends it: the body, then the end-of-turn contract."""
    return f"{body} {END_CONTRACT}"


SETTINGS = RunnerSettings(
    agents=(AgentKind.CLAUDE, AgentKind.CODEX),
    start_timeout_s=2,
    turn_timeout_s=2,
    poll_interval_s=0.05,
    timezone="UTC",
)


async def setup(
    *,
    crash_after_prompt: bool = False,
    codex_update_prompt: bool = False,
    resume_fails: bool = False,
    settings: RunnerSettings = SETTINGS,
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
        clock,
        crash_after_prompt=crash_after_prompt,
        codex_update_prompt=codex_update_prompt,
        resume_fails=resume_fails,
    )
    notifier = RecordingNotifier()
    runner = TaskRunner(
        store=store, host=host, adapters=ADAPTERS, notifier=notifier, clock=clock, settings=settings
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
        "AgentChosen",  # claude, first in preference order
        "AttemptStarted",  # claude
        "AttemptLocated",  # pane
        "AttemptLocated",  # claude's session id, learned from the pane
        "AttemptPrompted",
        "AttemptInterrupted",
        "AttemptEnded",
        "AgentChosen",  # codex, claude being limited
        "AttemptStarted",  # codex
        "AttemptLocated",
        "AttemptLocated",
        "AttemptPrompted",
        "AttemptEnded",
        "TaskCompleted",
    ]
    starts = [e for e in events if isinstance(e, AttemptStarted)]
    assert [e.agent for e in starts] == [AgentKind.CLAUDE, AgentKind.CODEX]
    choices = [e for e in events if isinstance(e, AgentChosen)]
    assert [(c.agent, c.reason) for c in choices] == [
        (AgentKind.CLAUDE, "claude: first in preference order, budget unknown."),
        (
            AgentKind.CODEX,
            "codex: next in preference order (claude limited until 2026-10-02 09:30), "
            "budget unknown.",
        ),
    ]

    limit = next(e for e in events if isinstance(e, AttemptInterrupted))
    assert limit.reason is InterruptReason.RATE_LIMITED
    # The fake prints "resets <now + 30 min>"; the adapter reads it back.
    assert limit.resume_not_before == NOW + timedelta(minutes=30)
    ended = [e for e in events if isinstance(e, AttemptEnded)]
    assert [e.outcome for e in ended] == [AttemptOutcome.ABANDONED, AttemptOutcome.SUCCEEDED]
    sessions = [e.session_ref for e in events if isinstance(e, AttemptLocated) and e.session_ref]
    assert sessions == ["claude-session", "codex-session"]

    observed = [(e.state, e.evidence) for e in events if isinstance(e, AgentStateObserved)]
    assert (AgentState.RATE_LIMITED, "baton:claude_usage_limit") in observed
    assert (AgentState.IDLE, "baton:codex_idle_prompt") in observed

    assert host.prompts == [
        (AgentKind.CLAUDE, sent(INSTRUCTIONS)),
        (AgentKind.CODEX, sent(f"{HANDOFF_NOTE} {INSTRUCTIONS}")),
    ]
    assert notifier.kinds == [
        NoticeKind.TASK_STARTED,
        NoticeKind.AGENT_LIMITED,
        NoticeKind.TASK_HANDED_OFF,
        NoticeKind.TASK_COMPLETED,
    ]
    assert notifier.notices[1].text == (
        "claude hit its usage limit. Available again at 2026-10-02 09:30 UTC."
    )

    board = project(await store.read())
    assert board.tasks[TASK].status is TaskStatus.COMPLETED
    availability = fold_availability(await store.read())
    assert availability.available([AgentKind.CLAUDE, AgentKind.CODEX], NOW) == [AgentKind.CODEX]
    assert any(isinstance(e, TaskCompleted) for e in events)


def only(agent: AgentKind) -> RunnerSettings:
    return replace(SETTINGS, agents=(agent,))


async def test_with_every_agent_limited_the_attempt_waits_and_resumes_its_own_session() -> None:
    store, host, notifier, _ = await setup()
    clock = FixedClock(NOW)
    runner = TaskRunner(
        store=store,
        host=host,
        adapters=ADAPTERS,
        notifier=notifier,
        clock=clock,
        settings=only(AgentKind.CLAUDE),
    )

    assert await runner.run(TASK) is TaskStatus.WAITING
    attempt = project(await store.read()).tasks[TASK].live_attempt
    assert attempt is not None
    assert attempt.interrupt_reason is InterruptReason.RATE_LIMITED  # kept, not abandoned
    assert notifier.kinds[-2:] == [NoticeKind.AGENT_LIMITED, NoticeKind.WAITING_FOR_AGENT]

    # Still limited: nothing happens.
    assert await runner.run(TASK) is TaskStatus.WAITING
    assert host.launched == ["claude"]

    # After the printed reset time the same session is resumed in a fresh pane.
    clock.advance(timedelta(minutes=31))
    assert await runner.run(TASK) is TaskStatus.COMPLETED

    assert host.launched == ["claude", "claude --resume claude-session"]
    assert host.prompts[-1] == (AgentKind.CLAUDE, sent(CONTINUE_NOTE))
    task = project(await store.read()).tasks[TASK]
    assert len(task.attempts) == 1  # one attempt, one conversation, across the limit
    assert task.attempts[0].outcome is AttemptOutcome.SUCCEEDED
    assert NoticeKind.TASK_RESUMED in notifier.kinds


async def test_a_crashed_agent_is_resumed_in_its_own_session() -> None:
    store, host, notifier, runner = await setup(crash_after_prompt=True)

    assert await runner.run(TASK) is TaskStatus.COMPLETED

    events = [s.event for s in await store.read()]
    interrupted = [e for e in events if isinstance(e, AttemptInterrupted)]
    assert [e.reason for e in interrupted] == [InterruptReason.CRASHED]
    assert host.launched == ["claude", "claude --resume claude-session"]
    assert notifier.kinds == [
        NoticeKind.TASK_STARTED,
        NoticeKind.TASK_STOPPED,
        NoticeKind.TASK_RESUMED,
        NoticeKind.TASK_COMPLETED,
    ]


async def test_without_resumes_left_a_crash_goes_to_a_person() -> None:
    store, host, notifier, _ = await setup(crash_after_prompt=True)
    runner = TaskRunner(
        store=store,
        host=host,
        adapters=ADAPTERS,
        notifier=notifier,
        clock=FixedClock(NOW),
        settings=replace(SETTINGS, max_failure_resumes=0),
    )

    assert await runner.run(TASK) is TaskStatus.WAITING
    assert host.launched == ["claude"]
    assert notifier.kinds[-1] is NoticeKind.NEEDS_HUMAN


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


async def test_a_session_that_cannot_be_reopened_is_restarted_fresh_on_the_same_agent() -> None:
    store, _, notifier, _ = await setup()
    host = SimulatedAgents(
        FixedClock(NOW),
        crash_after_prompt=True,
        resume_fails=True,
        scripts={AgentKind.CLAUDE: "claude-finish.toml"},
    )
    runner = TaskRunner(
        store=store,
        host=host,
        adapters=ADAPTERS,
        notifier=notifier,
        clock=FixedClock(NOW),
        settings=only(AgentKind.CLAUDE),
    )

    assert await runner.run(TASK) is TaskStatus.COMPLETED

    events = [s.event for s in await store.read()]
    reasons = [e.reason for e in events if isinstance(e, AttemptInterrupted)]
    assert reasons == [InterruptReason.CRASHED, InterruptReason.RESUME_FAILED]
    assert host.launched == ["claude", "claude --resume claude-session", "claude"]
    task = project(await store.read()).tasks[TASK]
    assert [a.outcome for a in task.attempts] == [
        AttemptOutcome.ABANDONED,
        AttemptOutcome.SUCCEEDED,
    ]
    # The fresh session is told that earlier work may already be in the directory.
    assert host.prompts[-1] == (AgentKind.CLAUDE, sent(f"{HANDOFF_NOTE} {INSTRUCTIONS}"))
    assert NoticeKind.TASK_RESTARTED in notifier.kinds


def claude_runner(
    store: InMemoryEventStore, host: SimulatedAgents, notifier: RecordingNotifier
) -> TaskRunner:
    return TaskRunner(
        store=store,
        host=host,
        adapters=ADAPTERS,
        notifier=notifier,
        clock=FixedClock(NOW),
        settings=only(AgentKind.CLAUDE),
    )


async def stop_after_prompt(store: InMemoryEventStore, runner: TaskRunner) -> None:
    """Run until the prompt is recorded, then stop the runner as a dying batond would."""
    run = asyncio.create_task(runner.run(TASK))
    while not any(isinstance(s.event, AttemptPrompted) for s in await store.read()):
        await asyncio.sleep(0.01)
    run.cancel()
    with suppress(asyncio.CancelledError):
        await run


async def test_after_a_daemon_restart_the_active_attempt_is_re_attached_not_restarted() -> None:
    store, _, notifier, _ = await setup()
    host = SimulatedAgents(FixedClock(NOW), scripts={AgentKind.CLAUDE: "claude-finish.toml"})
    await stop_after_prompt(store, claude_runner(store, host, notifier))
    await asyncio.sleep(0.1)  # the agent finishes its turn while nothing watches

    assert await claude_runner(store, host, notifier).run(TASK) is TaskStatus.COMPLETED

    # One launch, one prompt: the restart picked the attempt up where it was.
    assert host.launched == ["claude"]
    assert host.prompts == [(AgentKind.CLAUDE, sent(INSTRUCTIONS))]
    task = project(await store.read()).tasks[TASK]
    assert [a.outcome for a in task.attempts] == [AttemptOutcome.SUCCEEDED]


async def test_a_re_attached_attempt_whose_pane_is_gone_is_resumed() -> None:
    store, _, notifier, _ = await setup()
    host = SimulatedAgents(FixedClock(NOW), scripts={AgentKind.CLAUDE: "claude-finish.toml"})
    await stop_after_prompt(store, claude_runner(store, host, notifier))
    pane = project(await store.read()).tasks[TASK].attempts[0].pane_id
    assert pane is not None
    await host.close_pane(pane)

    assert await claude_runner(store, host, notifier).run(TASK) is TaskStatus.COMPLETED

    reasons = [
        s.event.reason for s in await store.read() if isinstance(s.event, AttemptInterrupted)
    ]
    assert reasons == [InterruptReason.CRASHED]
    assert host.launched == ["claude", "claude --resume claude-session"]
    assert host.prompts[-1] == (AgentKind.CLAUDE, sent(CONTINUE_NOTE))


async def test_a_blocked_attempt_is_reported_once_and_continues_once_a_person_answers() -> None:
    store, _, notifier, _ = await setup()
    host = SimulatedAgents(
        FixedClock(NOW),
        scripts={AgentKind.CLAUDE: "claude-finish.toml"},
        permission_after_prompt=True,
    )
    runner = claude_runner(store, host, notifier)

    assert await runner.run(TASK) is TaskStatus.NEEDS_HUMAN
    assert await runner.run(TASK) is TaskStatus.NEEDS_HUMAN  # still blocked: no second notice
    assert notifier.kinds.count(NoticeKind.NEEDS_HUMAN) == 1

    host.approve()
    assert await runner.run(TASK) is TaskStatus.COMPLETED
    assert host.prompts == [(AgentKind.CLAUDE, sent(INSTRUCTIONS))]


async def test_a_turn_that_ends_with_a_question_waits_for_a_person() -> None:
    store, _, notifier, _ = await setup()
    host = SimulatedAgents(
        FixedClock(NOW),
        scripts={AgentKind.CLAUDE: "claude-finish.toml"},
        end_mark="⏺ Should power(0, 0) return 1 or raise?\n  [[BATON:END status=question]]",
    )
    runner = claude_runner(store, host, notifier)

    assert await runner.run(TASK) is TaskStatus.NEEDS_HUMAN
    asked = [n for n in notifier.notices if n.kind is NoticeKind.NEEDS_HUMAN]
    assert [n.text for n in asked] == [
        "claude is waiting for a decision: Should power(0, 0) return 1 or raise?"
    ]
    assert await runner.run(TASK) is TaskStatus.NEEDS_HUMAN  # asked once, not every cycle
    assert notifier.kinds.count(NoticeKind.NEEDS_HUMAN) == 1

    # Answered at the terminal; the agent finishes and says so.
    host.end_mark = "⏺ It returns 1 now.\n  [[BATON:END status=done]]"
    host.reshow()
    assert await runner.run(TASK) is TaskStatus.COMPLETED
    assert len(host.prompts) == 1


async def test_notices_give_times_in_the_configured_zone() -> None:
    # The simulated Claude prints "resets 9:30am" (UTC's clock); read in Istanbul, where it
    # is already 12:00, that is 9:30 the next morning, and the notice says so in that zone.
    settings = replace(SETTINGS, timezone="Europe/Istanbul")
    _, _, notifier, runner = await setup(settings=settings)

    await runner.run(TASK)

    limited = next(n for n in notifier.notices if n.kind is NoticeKind.AGENT_LIMITED)
    assert limited.text == "claude hit its usage limit. Available again at 2026-10-03 09:30 +03."
