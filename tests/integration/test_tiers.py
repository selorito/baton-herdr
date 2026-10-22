"""Economy tasks run the same agents on a cheaper model (ADR 0020)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest

from baton_herdr.adapters import ADAPTERS
from baton_herdr.budget.reservation import expected_cost, session_costs
from baton_herdr.core.config import SchedulerSettings, TierSettings
from baton_herdr.core.events import (
    AgentChosen,
    AttemptEnded,
    AttemptLocated,
    AttemptStarted,
    TaskEscalated,
)
from baton_herdr.core.fakes import FixedClock, InMemoryEventStore, RecordingNotifier
from baton_herdr.core.model import (
    AgentKind,
    AttemptId,
    AttemptOutcome,
    InterruptReason,
    TaskStatus,
)
from baton_herdr.core.notify import NoticeKind
from baton_herdr.core.projection import InvalidEventError, project
from baton_herdr.core.routing import LaunchProfile, ModelSpec, Tier
from baton_herdr.core.usage import UsageRecord, UsageTokens
from baton_herdr.daemon import add_task, runner_settings
from baton_herdr.scheduler.backend import DirectBackend
from baton_herdr.scheduler.runner import RunnerSettings, TaskRunner

from detector_binary import rust_detector
from simulated_agents import SimulatedAgents

if TYPE_CHECKING:
    from baton_herdr.core.events import Event

NOW = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)
CLOCK = FixedClock(NOW)
CLAUDE, CODEX = AgentKind.CLAUDE, AgentKind.CODEX
HAIKU = {CLAUDE: ModelSpec(model="haiku")}


async def run(  # noqa: PLR0913 - one switch per setting a test varies
    *,
    tier: Tier | None = None,
    role_tiers: dict[str, Tier] | None = None,
    economy: dict[AgentKind, ModelSpec] | None = None,
    fallback: Literal["premium", "wait"] = "premium",
    crash: bool = False,
    max_failure_resumes: int = 2,
    notifier: RecordingNotifier | None = None,
) -> tuple[TaskStatus, SimulatedAgents, list[Event]]:
    store = InMemoryEventStore()
    task_id = await add_task(
        store, title="page", instructions="Write it.", workdir="/w", clock=CLOCK, tier=tier
    )
    host = SimulatedAgents(
        CLOCK,
        scripts={CLAUDE: "claude-finish.toml", CODEX: "codex-finish.toml"},
        crash_after_prompt=crash,
    )
    runner = TaskRunner(
        detector=rust_detector(),
        store=store,
        host=host,
        adapters=ADAPTERS,
        notifier=notifier or RecordingNotifier(),
        clock=CLOCK,
        settings=RunnerSettings(
            agents=(CODEX, CLAUDE),  # codex preferred
            max_failure_resumes=max_failure_resumes,
            start_timeout_s=2,
            turn_timeout_s=2,
            poll_interval_s=0.05,
            role_tiers=role_tiers or {},
            economy=economy or {},
            economy_fallback=fallback,
        ),
    )
    status = await runner.run(task_id)
    return status, host, [s.event for s in await store.read()]


def chosen(events: list[Event]) -> AgentChosen:
    (choice,) = [e for e in events if isinstance(e, AgentChosen)]
    return choice


async def test_without_tiers_nothing_changes() -> None:
    status, host, events = await run(economy=HAIKU)
    assert status is TaskStatus.COMPLETED
    assert host.launched == ["codex"]
    (started,) = [e for e in events if isinstance(e, AttemptStarted)]
    assert (started.tier, started.backend) == (Tier.PREMIUM, "direct")


async def test_an_economy_task_goes_first_to_an_agent_with_an_economy_profile() -> None:
    status, host, events = await run(role_tiers={"task": Tier.ECONOMY}, economy=HAIKU)
    assert status is TaskStatus.COMPLETED
    # Claude, though Codex is preferred: only Claude has an economy profile.
    assert host.launched == ["claude --model haiku"]
    (started,) = [e for e in events if isinstance(e, AttemptStarted)]
    assert (started.agent, started.tier) == (CLAUDE, Tier.ECONOMY)
    assert chosen(events).reason == (
        "claude: first in preference order, budget unknown. Economy: haiku."
    )


async def test_a_resumed_economy_session_stays_on_the_economy_model() -> None:
    status, host, _ = await run(tier=Tier.ECONOMY, economy=HAIKU, crash=True)
    assert status is TaskStatus.COMPLETED
    assert host.launched == [
        "claude --model haiku",
        f"{ADAPTERS[CLAUDE].resume_command('claude-session')} --model haiku",
    ]


async def test_a_task_given_a_tier_keeps_it_whatever_its_kind() -> None:
    _, host, _ = await run(tier=Tier.ECONOMY, economy=HAIKU)
    assert host.launched == ["claude --model haiku"]


async def test_flags_alone_make_an_economy_profile() -> None:
    flags = {CODEX: ModelSpec(args=("-c", "model_reasoning_effort=low"))}
    _, host, events = await run(tier=Tier.ECONOMY, economy=flags)
    assert host.launched == ["codex -c model_reasoning_effort=low"]
    assert chosen(events).reason.endswith("Economy: -c model_reasoning_effort=low.")


async def test_with_no_economy_profile_the_task_runs_premium_and_says_so() -> None:
    status, host, events = await run(tier=Tier.ECONOMY)
    assert status is TaskStatus.COMPLETED
    assert host.launched == ["codex"]
    assert chosen(events).reason.endswith("Premium: codex has no economy profile.")


async def test_with_no_economy_profile_a_waiting_task_starts_nothing() -> None:
    status, host, events = await run(tier=Tier.ECONOMY, fallback="wait")
    assert status is TaskStatus.PENDING
    assert host.launched == []
    choice = chosen(events)
    assert choice.agent is None
    assert choice.reason == "No agent has an economy profile ([tiers.economy]); the task waits."


def test_the_direct_backend_offers_economy_only_to_agents_named_for_it() -> None:
    direct = DirectBackend({CLAUDE: {"step": "sonnet"}}, HAIKU)
    assert direct.profile(CLAUDE, Tier.ECONOMY, "step") == LaunchProfile("direct", model="haiku")
    assert direct.profile(CODEX, Tier.ECONOMY, "step") is None
    # Premium is untouched by it.
    assert direct.profile(CLAUDE, Tier.PREMIUM, "step") == LaunchProfile("direct", model="sonnet")


def test_tiers_come_from_their_own_section() -> None:
    tiers = TierSettings.model_validate(
        {
            "roles": {"step": "economy"},
            "economy_fallback": "wait",
            "economy": {"claude": {"model": "haiku"}, "codex": {"args": ["-c", "x=1"]}},
        }
    )
    converted = runner_settings(SchedulerSettings(), tiers=tiers)
    assert converted.role_tiers == {"step": Tier.ECONOMY}
    assert converted.economy_fallback == "wait"
    assert converted.economy == {
        CLAUDE: ModelSpec(model="haiku"),
        CODEX: ModelSpec(args=("-c", "x=1")),
    }
    assert runner_settings(SchedulerSettings()).role_tiers == {}


async def test_a_log_from_before_tiers_reads_as_premium_on_direct() -> None:
    store = InMemoryEventStore()
    task_id = await add_task(store, title="t", instructions="-", workdir="/w", clock=CLOCK)
    attempt = AttemptId(uuid5(NAMESPACE_URL, "old"))
    await store.append(
        [AttemptStarted(occurred_at=NOW, task_id=task_id, attempt_id=attempt, agent=CODEX)]
    )
    task = project(await store.read()).tasks[task_id]
    assert task.tier is None
    assert (task.attempts[0].tier, task.attempts[0].backend) == (Tier.PREMIUM, "direct")


async def test_an_economy_attempt_is_expected_to_cost_what_economy_attempts_did() -> None:
    store = InMemoryEventStore()
    usage: list[UsageRecord] = []
    for n, (tier, tokens) in enumerate([(Tier.ECONOMY, 5_000)] * 3 + [(Tier.PREMIUM, 80_000)] * 3):
        task_id = await add_task(store, title=f"t{n}", instructions="-", workdir=f"/w/{n}")
        attempt = AttemptId(uuid5(NAMESPACE_URL, str(n)))
        await store.append(
            [
                AttemptStarted(
                    occurred_at=NOW, task_id=task_id, attempt_id=attempt, agent=CLAUDE, tier=tier
                ),
                AttemptLocated(
                    occurred_at=NOW, task_id=task_id, attempt_id=attempt, session_ref=f"s{n}"
                ),
                AttemptEnded(
                    occurred_at=NOW,
                    task_id=task_id,
                    attempt_id=attempt,
                    outcome=AttemptOutcome.SUCCEEDED,
                ),
            ]
        )
        usage.append(
            UsageRecord(
                agent="claude",
                session_id=f"s{n}",
                record_id=f"r{n}",
                at=NOW,
                tokens=UsageTokens(input=0, output=tokens, cache_read=0, cache_write=0),
            )
        )
    tasks = project(await store.read()).tasks.values()
    sessions = session_costs(usage)
    assert expected_cost(CLAUDE, "task", tasks, sessions, tier=Tier.ECONOMY) == 5_000
    assert expected_cost(CLAUDE, "task", tasks, sessions, tier=Tier.PREMIUM) == 80_000


async def test_an_economy_task_that_keeps_failing_moves_up_on_the_same_agent() -> None:
    notifier = RecordingNotifier()
    status, host, events = await run(
        tier=Tier.ECONOMY, economy=HAIKU, crash=True, max_failure_resumes=0, notifier=notifier
    )
    assert status is TaskStatus.COMPLETED
    # Claude again, though Codex is preferred, on its premium model in a fresh session.
    assert host.launched == ["claude --model haiku", "claude"]
    (escalated,) = [e for e in events if isinstance(e, TaskEscalated)]
    assert (escalated.agent, escalated.reason, escalated.failures) == (
        CLAUDE,
        InterruptReason.CRASHED,
        1,
    )
    started = [e for e in events if isinstance(e, AttemptStarted)]
    assert [e.tier for e in started] == [Tier.ECONOMY, Tier.PREMIUM]
    notice = next(n for n in notifier.notices if n.kind is NoticeKind.TASK_ESCALATED)
    assert notice.text == "⬆ Moved up to premium on claude."
    assert notice.details[0] == "In economy, claude crashed (economy attempt)."


async def test_a_crash_in_economy_is_resumed_there_before_anything_moves_up() -> None:
    status, host, events = await run(tier=Tier.ECONOMY, economy=HAIKU, crash=True)
    assert status is TaskStatus.COMPLETED
    assert not [e for e in events if isinstance(e, TaskEscalated)]
    assert host.launched[-1].endswith("--model haiku")


async def test_a_task_moves_up_once_and_only_between_attempts() -> None:
    store = InMemoryEventStore()
    task_id = await add_task(store, title="t", instructions="-", workdir="/w", clock=CLOCK)
    attempt = AttemptId(uuid5(NAMESPACE_URL, "live"))
    up = TaskEscalated(
        occurred_at=NOW, task_id=task_id, agent=CLAUDE, reason=InterruptReason.STALLED, failures=1
    )
    await store.append(
        [AttemptStarted(occurred_at=NOW, task_id=task_id, attempt_id=attempt, agent=CLAUDE)]
    )
    with pytest.raises(InvalidEventError, match="still live"):
        await store.append([up])
    await store.append(
        [
            AttemptEnded(
                occurred_at=NOW,
                task_id=task_id,
                attempt_id=attempt,
                outcome=AttemptOutcome.ABANDONED,
            ),
            up,
        ]
    )
    assert project(await store.read()).tasks[task_id].escalated == up
    with pytest.raises(InvalidEventError, match="moved up already"):
        await store.append([up.model_copy(update={"event_id": uuid4()})])


def test_escalation_comes_from_its_own_section() -> None:
    tiers = TierSettings.model_validate(
        {"escalation": {"after": 2, "on": ["stalled"], "economy_stall_minutes": 5}}
    )
    converted = runner_settings(SchedulerSettings(), tiers=tiers)
    assert converted.escalate_after == 2
    assert converted.escalate_on == frozenset({"stalled"})
    assert converted.economy_stall_after == timedelta(minutes=5)
