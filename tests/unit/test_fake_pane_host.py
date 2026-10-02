from __future__ import annotations

import asyncio

import pytest

from baton_herdr.core.fakes import FakePaneHost
from baton_herdr.core.model import AgentKind, AgentState
from baton_herdr.core.panes import (
    AgentBlockedError,
    AgentNotRunningError,
    PaneHost,
    PaneNotFoundError,
    PaneObservation,
)


def obs(state: AgentState, agent: AgentKind | None = AgentKind.CLAUDE) -> PaneObservation:
    return PaneObservation(pane_id="p1", agent=agent, state=state, evidence="test")


async def test_prompts_are_recorded_only_when_an_agent_can_take_them() -> None:
    host = FakePaneHost()
    pane_host: PaneHost = host
    with pytest.raises(PaneNotFoundError):
        await pane_host.send_prompt("p1", "hi")

    host.set_observation(obs(AgentState.UNKNOWN, agent=None))
    with pytest.raises(AgentNotRunningError):
        await pane_host.send_prompt("p1", "hi")

    host.set_observation(obs(AgentState.BLOCKED_PERMISSION))
    with pytest.raises(AgentBlockedError):
        await pane_host.send_prompt("p1", "hi")

    host.set_observation(obs(AgentState.IDLE))
    await pane_host.send_prompt("p1", "hi")
    await pane_host.send_keys("p1", ["Enter"])
    assert host.sent == [("p1", "prompt", "hi"), ("p1", "keys", ("Enter",))]


async def test_watch_yields_the_current_observation_then_changes() -> None:
    host = FakePaneHost()
    host.set_observation(obs(AgentState.IDLE))
    stream = host.watch("p1")

    assert (await anext(stream)).state is AgentState.IDLE
    host.set_observation(obs(AgentState.WORKING))
    assert (await asyncio.wait_for(anext(stream), 1)).state is AgentState.WORKING
    await stream.aclose()


async def test_screens_and_pane_lifecycle() -> None:
    host = FakePaneHost()
    pane_id = await host.open_pane(cwd="/work", label="task")
    host.screens[pane_id] = "one\ntwo\nthree\n"

    assert await host.read_screen(pane_id, lines=2) == "two\nthree\n"
    assert (await host.observe(pane_id)).cwd == "/work"
    await host.close_pane(pane_id)
    with pytest.raises(PaneNotFoundError):
        await host.observe(pane_id)
