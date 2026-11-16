"""HerdrPaneHost.watch under lost events, against a stand-in herdr with many panes.

herdr's events only say that a pane changed; the read after them is authoritative. When
herdr reports ``events_lost``, or a subscription's connection ends, the watch subscribes
again and reads the pane, so whatever changed in the gap still reaches the caller.
"""

from __future__ import annotations

import asyncio
import json
import random
from typing import TYPE_CHECKING, Any

import pytest

from baton_herdr.core.model import AgentState
from baton_herdr.core.panes import PaneHostUnavailableError
from baton_herdr.herdr.host import HerdrPaneHost
from baton_herdr.herdr.transport import HerdrSocket

from platform_marks import posix_only

pytestmark = posix_only("the fake herdr server listens on a Unix socket")

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from baton_herdr.core.panes import PaneObservation

# herdr's status, the rule its explain names, and the state baton reads from them.
STATES = [
    ("idle", "live_prompt_box", AgentState.IDLE),
    ("working", "live_turn_working", AgentState.WORKING),
    ("blocked", "bash_permission_prompt", AgentState.BLOCKED_OTHER),
]


class ManyPanes:
    """A stand-in herdr: per-pane states, one queue of pushes per open subscription.

    ``change`` sets a pane's state and tells its subscribers in one of herdr's ways: an
    event, a burst of events, ``events_lost``, or the end of the connection.
    """

    def __init__(self) -> None:
        self.states: dict[str, int] = {}
        self.subscribers: dict[str, list[asyncio.Queue[dict[str, Any] | None]]] = {}
        self.subscriptions = 0
        self.refuse_subscriptions = False
        self.handlers: set[asyncio.Task[None]] = set()

    def change(self, pane_id: str, state: int, how: str) -> None:
        self.states[pane_id] = state
        event = {"event": "pane.agent_status_changed", "data": {"pane_id": pane_id}}
        for queue in self.subscribers.get(pane_id, []):
            if how == "event":
                queue.put_nowait(event)
            elif how == "burst":
                for _ in range(5):
                    queue.put_nowait(event)
            elif how == "lost":
                queue.put_nowait({"error": {"code": "events_lost", "message": "lagged"}})
            else:  # "closed": the connection ends without a word
                queue.put_nowait(None)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self.handlers.add(task)
        request = json.loads(await reader.readline())
        params = request["params"]

        def send(body: dict[str, Any]) -> None:
            writer.write(json.dumps({"id": request["id"], **body}).encode() + b"\n")

        if request["method"] == "events.subscribe":
            if self.refuse_subscriptions:
                writer.close()
                return
            pane_id = params["subscriptions"][0]["pane_id"]
            queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
            self.subscribers.setdefault(pane_id, []).append(queue)
            self.subscriptions += 1
            try:
                send({"result": {"type": "subscription_started"}})
                await writer.drain()
                while (pushed := await queue.get()) is not None:
                    if "error" in pushed:
                        send(pushed)
                    else:
                        writer.write(json.dumps(pushed).encode() + b"\n")
                    await writer.drain()
                    if "error" in pushed:
                        break
            finally:
                self.subscribers[pane_id].remove(queue)
        else:
            pane_id = params.get("pane_id") or params["target"]
            status, rule, _ = STATES[self.states[pane_id]]
            if request["method"] == "pane.get":
                pane = {"pane_id": pane_id, "agent": "claude", "agent_status": status}
                send({"result": {"type": "pane_info", "pane": pane}})
            else:  # agent.explain
                explain = {"state": status, "matched_rule": {"id": rule}}
                send({"result": {"type": "agent_explain", "explain": explain}})
            await writer.drain()
        writer.close()


@pytest.fixture
async def herdr(tmp_path: Path) -> AsyncIterator[tuple[ManyPanes, HerdrPaneHost]]:
    stand_in = ManyPanes()
    socket_path = tmp_path / "herdr.sock"
    server = await asyncio.start_unix_server(stand_in.handle, path=str(socket_path))
    async with server:
        yield stand_in, HerdrPaneHost(HerdrSocket(socket_path, timeout_s=5))
        for handler in stand_in.handlers:
            handler.cancel()


async def next_state(stream: AsyncIterator[PaneObservation]) -> AgentState:
    return (await asyncio.wait_for(anext(stream), 5)).state


async def test_a_subscription_whose_connection_ends_is_opened_again_and_the_pane_read(
    herdr: tuple[ManyPanes, HerdrPaneHost],
) -> None:
    stand_in, host = herdr
    stand_in.states["w1:p1"] = 1
    stream = host.watch("w1:p1")
    assert await next_state(stream) is AgentState.WORKING

    stand_in.change("w1:p1", 0, "closed")  # the turn ends as the connection does

    assert await next_state(stream) is AgentState.IDLE
    assert stand_in.subscriptions == 2
    await stream.aclose()


async def test_a_subscription_herdr_does_not_take_again_is_reported_as_unavailable(
    herdr: tuple[ManyPanes, HerdrPaneHost],
) -> None:
    stand_in, host = herdr
    stand_in.states["w1:p1"] = 1
    stream = host.watch("w1:p1")
    assert await next_state(stream) is AgentState.WORKING

    stand_in.refuse_subscriptions = True
    stand_in.change("w1:p1", 0, "closed")

    with pytest.raises(PaneHostUnavailableError, match="herdr closed the connection"):
        await next_state(stream)


@pytest.mark.parametrize("seed", range(4))
async def test_many_watched_panes_end_on_their_current_state_whatever_is_lost(
    herdr: tuple[ManyPanes, HerdrPaneHost], seed: int
) -> None:
    stand_in, host = herdr
    rng = random.Random(seed)  # noqa: S311 - reproducible losses, not secrets
    panes = [f"w1:p{n}" for n in range(24)]
    for pane_id in panes:
        stand_in.states[pane_id] = 0
    last: dict[str, AgentState] = {}

    async def follow(pane_id: str) -> None:
        async for observation in host.watch(pane_id):
            last[pane_id] = observation.state

    async with asyncio.TaskGroup() as group:
        followers = [group.create_task(follow(pane_id)) for pane_id in panes]
        async with asyncio.timeout(10):
            while stand_in.subscriptions < len(panes):
                await asyncio.sleep(0.01)
        for _ in range(400):
            how = rng.choices(["event", "burst", "lost", "closed"], [5, 2, 2, 1])[0]
            stand_in.change(rng.choice(panes), rng.randrange(len(STATES)), how)
            if rng.random() < 0.2:
                await asyncio.sleep(0)  # some changes land between reads, some in a burst
        expected = {pane_id: STATES[stand_in.states[pane_id]][2] for pane_id in panes}
        async with asyncio.timeout(15):
            while last != expected:
                assert not any(f.done() for f in followers), "a watch ended"
                await asyncio.sleep(0.02)
        for follower in followers:
            follower.cancel()
