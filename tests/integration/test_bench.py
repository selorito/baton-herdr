"""The benchmark runs baton's real loop on simulated time, and repeats itself exactly."""

from __future__ import annotations

import asyncio
import time

from baton_bench import report, scenarios
from baton_bench.sim import Budget, simulate
from baton_bench.vtime import VirtualTimeLoop
from baton_herdr.core.model import TaskStatus


def test_simulated_time_jumps_ahead_when_everything_waits() -> None:
    async def an_hour() -> float:
        loop = asyncio.get_running_loop()
        await asyncio.sleep(3_600)
        return loop.time()

    started = time.monotonic()
    assert asyncio.run(an_hour(), loop_factory=VirtualTimeLoop) >= 3_600
    assert time.monotonic() - started < 1


def test_a_limit_moves_the_task_and_nothing_is_lost_twice() -> None:
    result = simulate(scenarios.limit_mid_task, "test/limit")
    (task,) = result.tasks
    assert task.status is TaskStatus.COMPLETED
    assert task.attempts == 2  # Claude, then Codex
    assert task.redone <= 1  # at most the step Claude was in
    assert [kind for kind, _ in result.detection] == ["limit"]


def test_every_scenario_settles() -> None:
    for scenario in scenarios.SCENARIOS:
        (task,) = simulate(scenario.build, f"test/{scenario.name}").tasks
        assert task.status is TaskStatus.COMPLETED or task.needs_person, scenario.name


def test_the_same_seed_gives_the_same_report() -> None:
    def once() -> str:
        worlds = [simulate(scenarios.all_limited, f"test/same/{n}") for n in range(3)]
        return "\n".join(report.scenario_table([("all", report.summarize(worlds))]))

    assert once() == once()


def test_budget_modes_play_the_same_world() -> None:
    off = simulate(lambda rng: scenarios.queue(rng, Budget(collect_usage=False)), "test/q")
    on = simulate(lambda rng: scenarios.queue(rng, Budget(configured_window=True)), "test/q")
    assert [t.steps for t in off.tasks] == [t.steps for t in on.tasks]
