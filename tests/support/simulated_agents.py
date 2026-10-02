"""Scripted Claude and Codex agents on a fake pane host, shared by integration tests.

The agents play the same scripts as tools/fake-agent, and the pane host reports
the states herdr reports for those screens (rule-based idle for Claude, the idle
fallback for Codex), so the adapters classify exactly what they would live.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from coban.adapters import ADAPTERS
from coban.core.fakes import FakePaneHost
from coban.core.model import AgentKind, AgentState
from coban.core.panes import PaneObservation

import fake_agent

if TYPE_CHECKING:
    from collections.abc import Sequence

    from coban.core.ports import Clock

SCRIPTS = Path(__file__).parents[2] / "tools" / "fake-agent" / "scripts"

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

    def __init__(self, clock: Clock, *, crash_after_prompt: bool = False) -> None:
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
