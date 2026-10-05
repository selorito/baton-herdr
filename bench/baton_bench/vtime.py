"""An asyncio event loop on simulated time, so a run is fast and repeatable.

Nothing in a benchmark run does real I/O: the event store, the pane host and the agents
are in memory. When every coroutine waits, the loop does not sleep until the next timer;
it moves its clock there. Hours of agent work and waiting for limits to reset take
seconds, and the order of events depends on nothing but the program and its seed.
"""

from __future__ import annotations

import asyncio
import selectors
from datetime import datetime, timedelta


class SimulationStalledError(RuntimeError):
    """Every coroutine waits and no timer is set: nothing can ever happen again."""


class _Selector(selectors.DefaultSelector):
    def __init__(self) -> None:
        super().__init__()
        self.now = 0.0

    def select(self, timeout: float | None = None) -> list[tuple[selectors.SelectorKey, int]]:
        ready = super().select(0)  # the loop's own wake-up pipe, nothing else
        if ready:
            return ready
        if timeout is None:
            raise SimulationStalledError
        self.now += max(0.0, timeout)
        return []


class VirtualTimeLoop(asyncio.SelectorEventLoop):
    def __init__(self) -> None:
        self._virtual = _Selector()
        super().__init__(self._virtual)

    def time(self) -> float:
        return self._virtual.now


class VirtualClock:
    """baton's ``Clock`` port on the loop's simulated time."""

    def __init__(self, start: datetime) -> None:
        self._start = start

    def now(self) -> datetime:
        return self._start + timedelta(seconds=asyncio.get_running_loop().time())
