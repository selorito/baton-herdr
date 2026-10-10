"""The Telegram bot is started again after it fails, so a network outage at boot does not
leave batond without approvals and replies until it restarts."""

from __future__ import annotations

import asyncio

import pytest

from baton_herdr.core.backoff import Backoff
from baton_herdr.telegram.bot import keep_running

BACKOFF = Backoff(first_s=1, max_s=4, healthy_s=60)


class Script:
    """Runs of the bot: each fails after a given time, or the last one returns."""

    def __init__(self, *runs: float | None) -> None:
        self.runs = list(runs)
        self.now = 0.0
        self.waits: list[float] = []

    async def start(self) -> None:
        took = self.runs.pop(0)
        if took is None:
            return
        self.now += took
        raise ConnectionError("Cannot connect to host api.telegram.org")

    async def sleep(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.now += seconds

    def clock(self) -> float:
        return self.now


async def test_a_failing_bot_is_started_again_waiting_longer_each_time() -> None:
    script = Script(0, 0, 0, 0, None)
    await keep_running(script.start, backoff=BACKOFF, sleep=script.sleep, clock=script.clock)
    assert script.waits == [1, 2, 4, 4]


async def test_a_failure_after_a_healthy_run_waits_the_shortest_time_again() -> None:
    script = Script(0, 0, 3600, None)
    await keep_running(script.start, backoff=BACKOFF, sleep=script.sleep, clock=script.clock)
    assert script.waits == [1, 2, 1]


async def test_cancelling_stops_it_without_an_error_of_its_own() -> None:
    started = asyncio.Event()

    async def polls_forever() -> None:
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(keep_running(polls_forever, backoff=BACKOFF))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
