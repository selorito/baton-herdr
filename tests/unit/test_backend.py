"""Agents start from a backend's launch profile (ADR 0020)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from baton_herdr.adapters import ADAPTERS
from baton_herdr.core.events import TaskCreated
from baton_herdr.core.fakes import FakeBackend, FixedClock, InMemoryEventStore, RecordingNotifier
from baton_herdr.core.model import AgentKind, TaskId, TaskStatus
from baton_herdr.core.routing import LaunchProfile, Tier
from baton_herdr.scheduler.backend import DirectBackend
from baton_herdr.scheduler.runner import LaunchProfileError, RunnerSettings, TaskRunner

from detector_binary import rust_detector
from simulated_agents import SimulatedAgents

NOW = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)
CLOCK = FixedClock(NOW)
CODEX = AgentKind.CODEX
TASK = TaskId("t-1")


async def test_direct_premium_is_the_model_set_for_the_kind_of_task() -> None:
    direct = DirectBackend({CODEX: {"step": "fast"}})
    assert direct.profile(CODEX, Tier.PREMIUM, "step") == LaunchProfile("direct", model="fast")
    assert direct.profile(CODEX, Tier.PREMIUM, "task") == LaunchProfile("direct")
    assert direct.profile(AgentKind.CLAUDE, Tier.PREMIUM, "step") == LaunchProfile("direct")
    assert (await direct.health()).up


def test_direct_offers_no_economy_yet() -> None:
    assert DirectBackend().profile(CODEX, Tier.ECONOMY, "step") is None


async def runner_with(backend: FakeBackend) -> tuple[TaskRunner, SimulatedAgents]:
    store = InMemoryEventStore()
    await store.append(
        [
            TaskCreated(
                occurred_at=NOW, task_id=TASK, title="power", instructions="Add it.", workdir="/w"
            )
        ]
    )
    host = SimulatedAgents(CLOCK, scripts={CODEX: "codex-finish.toml"})
    runner = TaskRunner(
        detector=rust_detector(),
        store=store,
        host=host,
        adapters=ADAPTERS,
        notifier=RecordingNotifier(),
        clock=CLOCK,
        settings=RunnerSettings(
            agents=(CODEX,), start_timeout_s=2, turn_timeout_s=2, poll_interval_s=0.05
        ),
        backend=backend,
    )
    return runner, host


async def test_the_agent_starts_with_the_profiles_model_and_flags() -> None:
    profile = LaunchProfile("fake", model="small one", args=("-c", "model_reasoning_effort=low"))
    backend = FakeBackend({(CODEX, Tier.PREMIUM, "task"): profile})
    runner, host = await runner_with(backend)

    assert await runner.run(TASK) is TaskStatus.COMPLETED
    assert host.launched == ["codex --model 'small one' -c model_reasoning_effort=low"]
    assert set(backend.asked) == {(CODEX, Tier.PREMIUM, "task"), (CODEX, Tier.ECONOMY, "task")}


@pytest.mark.parametrize(
    "profile",
    [None, LaunchProfile("gateway", env={"OPENAI_BASE_URL": "http://localhost:1"})],
    ids=["no profile", "needs a pane environment"],
)
async def test_a_profile_the_runner_cannot_apply_starts_nothing(
    profile: LaunchProfile | None,
) -> None:
    runner, host = await runner_with(FakeBackend({(CODEX, Tier.PREMIUM, "task"): profile}))

    with pytest.raises(LaunchProfileError):
        await runner.run(TASK)
    assert host.launched == []
