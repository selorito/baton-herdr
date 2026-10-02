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
UPDATE_DIALOG = (
    Path(__file__).parents[2]
    / "fixtures/codex/blocked_question/20260930T075645Z/screen.detection.txt"
)
# A session resumed with the adapter's resume command plays this script instead.
RESUMED_SCRIPT_FILES = {
    AgentKind.CLAUDE: "claude-finish.toml",
    AgentKind.CODEX: "codex-finish.toml",
}
SCRIPT_FILES = {AgentKind.CLAUDE: "claude-limit.toml", AgentKind.CODEX: "codex-finish.toml"}


class SimulatedAgents(FakePaneHost):
    """A pane host whose panes run scripted agents when their launch command is typed."""

    def __init__(  # noqa: PLR0913 - one switch per simulated behaviour
        self,
        clock: Clock,
        *,
        crash_after_prompt: bool = False,
        codex_update_prompt: bool = False,
        resume_fails: bool = False,
        scripts: dict[AgentKind, str] | None = None,
        permission_after_prompt: bool = False,
        end_mark: str | None = None,
    ) -> None:
        super().__init__()
        self._clock = clock
        self._crash_after_prompt = crash_after_prompt
        # Show Codex's real update dialog (a recorded screen) before its prompt.
        self._codex_update_prompt = codex_update_prompt
        # `claude --resume` answers "No conversation found" and exits, as seen live.
        self._resume_fails = resume_fails
        # Ask for a permission once the prompt arrives; ``approve`` lets the turn go on.
        self._permission_after_prompt = permission_after_prompt
        self._waiting_for_approval: dict[str, str] = {}
        # Closing lines of a finished turn: the agent's message and its end mark.
        self.end_mark = end_mark
        # Script played by a freshly launched agent; resumed sessions always finish.
        self._fresh_scripts = SCRIPT_FILES | (scripts or {})
        self.dialog_answers: list[tuple[str, ...]] = []
        self._typed: dict[str, str] = {}
        self._running: dict[str, tuple[AgentKind, int]] = {}
        self._scripts: dict[str, str] = {}
        self.launched: list[str] = []
        self.prompts: list[tuple[AgentKind, str]] = []
        self._tasks: list[asyncio.Task[None]] = []
        self._dialogs: set[str] = set()

    async def send_text(self, pane_id: str, text: str) -> None:
        await super().send_text(pane_id, text)
        self._typed[pane_id] = text

    async def send_keys(self, pane_id: str, keys: Sequence[str]) -> None:
        await super().send_keys(pane_id, keys)
        command = self._typed.pop(pane_id, "")
        agent, script = _command_target(command, self._fresh_scripts)
        if list(keys) == ["Enter"] and agent is not None and script is not None:
            self.launched.append(command)
            if (
                self._resume_fails
                and script == RESUMED_SCRIPT_FILES.get(agent)
                and "resume" in command
            ):
                refusal = f"No conversation found with session ID: {agent.value}-session"
                self.screens[pane_id] = f"$ {command}\n{refusal}\n$ \n"
                self.set_observation(
                    PaneObservation(
                        pane_id=pane_id,
                        agent=None,
                        state=AgentState.UNKNOWN,
                        evidence="herdr:no-agent",
                    )
                )
                return
            self._running[pane_id] = (agent, 0)
            self._scripts[pane_id] = script
            if agent is AgentKind.CODEX and self._codex_update_prompt:
                self._show_update_dialog(pane_id)
                return
            self._show(pane_id, prompt="")
        elif pane_id in self._dialogs:
            self.dialog_answers.append(tuple(keys))
            self._dialogs.discard(pane_id)
            self._show(pane_id, prompt="")

    def _show_update_dialog(self, pane_id: str) -> None:
        self._dialogs.add(pane_id)
        self.screens[pane_id] = UPDATE_DIALOG.read_text()
        self.set_observation(
            PaneObservation(
                pane_id=pane_id,
                agent=AgentKind.CODEX,
                state=AgentState.UNKNOWN,
                evidence="herdr:idle-fallback",
                session_ref="codex-session",
            )
        )

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
            self._crash_after_prompt = False  # crash once; a resumed session works
            self.set_observation(
                PaneObservation(
                    pane_id=pane_id, agent=None, state=AgentState.UNKNOWN, evidence="herdr:no-agent"
                )
            )
            return
        if self._permission_after_prompt:
            self._permission_after_prompt = False
            self._waiting_for_approval[pane_id] = prompt
            self.screens[pane_id] = "Bash command\n  rm -rf build\nDo you want to proceed?\n"
            self.set_observation(
                PaneObservation(
                    pane_id=pane_id,
                    agent=agent,
                    state=AgentState.BLOCKED_OTHER,
                    evidence="herdr:rule:bash_permission_prompt",
                    session_ref=f"{agent.value}-session",
                )
            )
            return
        self._running[pane_id] = (agent, 2)
        self._show(pane_id, prompt=prompt)

    def reshow(self) -> None:
        """Redraw every running agent, e.g. after a test changed ``end_mark``."""
        for pane_id in self._running:
            self._show(pane_id, prompt="")

    def approve(self) -> None:
        """A person answers the permission prompt at the terminal; the turn finishes."""
        for pane_id, prompt in self._waiting_for_approval.items():
            agent, _ = self._running[pane_id]
            self._running[pane_id] = (agent, 2)
            self._show(pane_id, prompt=prompt)
        self._waiting_for_approval.clear()

    def _show(self, pane_id: str, *, prompt: str) -> None:
        agent, step = self._running[pane_id]
        script = fake_agent.load_script(SCRIPTS / self._scripts[pane_id])
        screen = fake_agent.render(script.steps[step].screen, now=self._clock.now(), prompt=prompt)
        if step == len(HOST_STATES[agent]) - 1 and self.end_mark:
            screen = f"{self.end_mark}\n{screen}"
        self.screens[pane_id] = screen
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


def _command_target(
    command: str, fresh_scripts: dict[AgentKind, str]
) -> tuple[AgentKind | None, str | None]:
    """Which agent and script a typed command starts: a fresh launch or a resume."""
    for agent, adapter in ADAPTERS.items():
        if agent not in fresh_scripts:
            continue
        if command == adapter.launch_command():
            return agent, fresh_scripts[agent]
        if command == adapter.resume_command(f"{agent.value}-session"):
            return agent, RESUMED_SCRIPT_FILES[agent]
    return None, None
