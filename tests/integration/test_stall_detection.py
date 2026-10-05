"""A hang is found from a still screen and no tokens; a long response is not one (ADR 0014).

The runner runs on simulated time against the benchmark's simulated agents.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from baton_bench import scenarios, world
from baton_bench.sim import Setup, simulate
from baton_bench.world import Quota, Workdir
from baton_herdr.core.model import AgentKind, TaskStatus

if TYPE_CHECKING:
    import random

    import pytest


def test_a_hang_is_found_in_15_minutes_and_says_why() -> None:
    result = simulate(scenarios.hang, "test/hang")
    (task,) = result.tasks
    assert task.status is TaskStatus.COMPLETED
    ((kind, seconds),) = result.detection
    assert kind == "hang"
    assert seconds <= 15 * 60 + 5  # the screen last changed at the step's start
    assert result.interruptions == (
        (
            "stalled",
            "Working for 15 min with no change on screen and no tokens recorded for its session.",
        ),
    )


def test_without_usage_the_screen_alone_decides_after_30_minutes() -> None:
    result = simulate(scenarios.hang_without_usage, "test/hang")
    ((_, seconds),) = result.detection
    assert 15 * 60 < seconds <= 30 * 60 + 5
    ((reason, detail),) = result.interruptions
    assert reason == "stalled"
    assert detail == "Working for 30 min with no change on screen (usage is not collected)."


def test_a_long_response_is_not_a_hang() -> None:
    for n in range(5):
        result = simulate(scenarios.long_response, f"test/long/{n}")
        assert result.interruptions == ()
        assert result.tasks[0].status is TaskStatus.COMPLETED


def test_tokens_are_progress_while_the_screen_stays_the_same(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    show = world.World._show

    def still(self: world.World, pane_id: str, body: str, *, working: bool) -> None:
        show(self, pane_id, "● Working." if working else body, working=working)

    monkeypatch.setattr(world.World, "_show", still)

    def build(rng: random.Random) -> Setup:
        del rng
        quotas = {AgentKind.CLAUDE: Quota(cap=10**9), AgentKind.CODEX: Quota(cap=10**9)}
        return Setup(tasks=[Workdir(steps=25)], quotas=quotas)  # ~30 min of a still screen

    result = simulate(build, "test/still")
    assert result.interruptions == ()
    assert result.tasks[0].status is TaskStatus.COMPLETED
