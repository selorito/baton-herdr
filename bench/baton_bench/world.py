"""Simulated agents, their quotas and the tasks' working directories, behind herdr's port.

The model, kept small on purpose (bench/README.md says why each part is there):

- A task is a number of **steps**. A step is one edit the agent makes and keeps: once it
  is done it is in the working directory, whichever agent did it.
- An agent works through the steps that are not done yet. The step it is in when it stops
  (a limit, a crash, a hang) is lost: its tokens are spent, its edit is not there.
- A **fresh session** first reads the task and the work so far; a **resumed session**
  remembers it and only reads its cached context again. Both cost tokens, not steps.
- Each agent has a **quota**: so many tokens per five-hour window, counted the way the
  limits count them (input, cache writes and output). A response that would go past it
  stops halfway, and the agent shows its limit message with the time the window ends.
- The **collector** sees every response: Claude's as usage records, Codex's also as its
  own report of how much of its window is used. Only when the scenario turns it on.

The screens are the fake agent's (tools/fake-agent/), and the states herdr reports for
them are the ones recorded live (tests/integration/test_fake_agent_live.py), so baton's
adapters classify what they would classify for real.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING

from baton_herdr.adapters import ADAPTERS
from baton_herdr.core.fakes import FakePaneHost
from baton_herdr.core.model import AgentKind, AgentState
from baton_herdr.core.panes import PaneObservation
from baton_herdr.core.usage import (
    RateLimitObservation,
    RateLimitWindow,
    UsageRecord,
    UsageTokens,
)

if TYPE_CHECKING:
    import random
    from collections.abc import Coroutine, Sequence
    from datetime import datetime

    from baton_herdr.core.ports import Clock, UsageStore

WINDOW = timedelta(hours=5)
# An agent with no quota left answers a prompt with its limit message this fast.
LIMIT_REPLY_S = 2.0


@dataclass(frozen=True, slots=True)
class Profile:
    """How long an agent takes and what it spends, as ranges drawn from uniformly."""

    launch_s: tuple[float, float] = (3, 8)
    step_s: tuple[float, float] = (40, 120)
    input: tuple[int, int] = (500, 3_000)
    cache_write: tuple[int, int] = (2_000, 8_000)
    output: tuple[int, int] = (300, 2_500)
    cache_read: tuple[int, int] = (20_000, 80_000)
    # A fresh session reads the task, then each step done so far, at this share of a step.
    read_task_steps: float = 1.0
    read_done_share: float = 0.5
    # A resumed session reads its cached context again: this share of a step, as cache reads.
    resume_share: float = 0.2


@dataclass(slots=True)
class Quota:
    """Tokens per five-hour window, opened by the first response after the last one ended."""

    cap: int
    start: datetime | None = None
    used: int = 0

    def _roll(self, now: datetime) -> None:
        if self.start is not None and now >= self.start + WINDOW:
            self.start, self.used = None, 0

    def limited_until(self, now: datetime) -> datetime | None:
        self._roll(now)
        if self.start is not None and self.used >= self.cap:
            return self.start + WINDOW
        return None

    def room(self, now: datetime) -> int:
        self._roll(now)
        return self.cap - self.used

    def spend(self, now: datetime, tokens: int) -> None:
        self._roll(now)
        if self.start is None:
            self.start = now
        self.used += tokens


@dataclass(frozen=True, slots=True)
class Fault:
    # "crash" or "hang"; or "slow": a step that is one long response, with a still screen
    # and no tokens until it ends. Not a fault of the agent, but one a watcher could mistake.
    kind: str
    step: int  # it happens during this step (0-based)
    seconds: float = 0  # for "slow": how long the step takes


@dataclass(slots=True)
class Workdir:
    """One task's working directory: how far the work is, and what it cost."""

    steps: int
    faults: list[Fault] = field(default_factory=list)
    done: int = 0
    started: int = 0  # steps begun, the lost ones included
    tokens: int = 0  # counted tokens spent on the task
    wasted: int = 0  # of those, on steps that were lost
    overhead: int = 0  # of those, on reading the task and the work so far

    @property
    def redone(self) -> int:
        return self.started - self.done

    def fault_at(self, step: int) -> Fault | None:
        for fault in self.faults:
            if fault.step == step:
                self.faults.remove(fault)
                return fault
        return None


@dataclass(frozen=True, slots=True)
class Incident:
    """Something that stopped an agent, for the latency measurements."""

    kind: str  # "limit", "crash" or "hang"
    workdir: str
    agent: AgentKind
    at: datetime
    resets_at: datetime | None = None


@dataclass(slots=True)
class _Process:
    agent: AgentKind
    session: str
    workdir: str
    fresh: bool  # a new session that has not read the task yet
    frozen: bool = False


# Screens, after tools/fake-agent/scripts/; herdr's state for each, as recorded live.
_CLAUDE_FRAME = """
{body}
────────────────────────────────────────
❯
────────────────────────────────────────
  ⏸ manual mode on · {footer}
"""
_CODEX_FRAME = """
{body}
› Ask Codex to do anything
  fake-model · ~/work{spinner}
"""
_IDLE = (AgentState.IDLE, "herdr:rule:live_prompt_box")
_WORKING = (AgentState.WORKING, "herdr:rule:live_turn_working")
_CODEX_IDLE = (AgentState.UNKNOWN, "herdr:idle-fallback")
_CODEX_WORKING = (AgentState.WORKING, "herdr:rule:screen_working_fallback")


def _screen(agent: AgentKind, body: str, *, working: bool) -> tuple[str, AgentState, str]:
    if agent is AgentKind.CLAUDE:
        footer = "esc to interrupt" if working else "? for shortcuts"
        state, evidence = _WORKING if working else _IDLE
        return _CLAUDE_FRAME.format(body=body, footer=footer), state, evidence
    state, evidence = _CODEX_WORKING if working else _CODEX_IDLE
    spinner = " · ⠏" if working else ""
    return _CODEX_FRAME.format(body=body, spinner=spinner), state, evidence


def _clock(at: datetime) -> str:
    """``3:45pm``, rounded up to the minute, as Claude prints a reset time."""
    at = _ceil_minute(at)
    hour = at.hour % 12 or 12
    return f"{hour}:{at.minute:02d}{'pm' if at.hour >= 12 else 'am'}"


def _date(at: datetime) -> str:
    """``Oct 5th, 2026 9:01 PM``, rounded up to the minute, as Codex prints it."""
    at = _ceil_minute(at)
    day = at.day
    suffix = "th" if 11 <= day % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    hour = at.hour % 12 or 12
    return (
        f"{at:%b} {day}{suffix}, {at.year} {hour}:{at.minute:02d} {'PM' if at.hour >= 12 else 'AM'}"
    )


def _ceil_minute(at: datetime) -> datetime:
    rounded = at.replace(second=0, microsecond=0)
    return rounded if rounded == at else rounded + timedelta(minutes=1)


def _limit_text(agent: AgentKind, resets_at: datetime) -> str:
    if agent is AgentKind.CLAUDE:
        return (
            "● Working on the task.\n"
            f"  ⎿  You've hit your session limit · resets {_clock(resets_at)}\n"
            "     /upgrade to increase your usage limit."
        )
    return f"■ You've hit your usage limit. Upgrade to Pro or try again at {_date(resets_at)}."


class World(FakePaneHost):
    """herdr, as baton sees it, with simulated agents in its panes."""

    def __init__(  # noqa: PLR0913 - the world's parts, each set by the scenario
        self,
        *,
        clock: Clock,
        rng: random.Random,
        quotas: dict[AgentKind, Quota],
        workdirs: dict[str, Workdir],
        profiles: dict[AgentKind, Profile] | None = None,
        usage: UsageStore | None = None,
    ) -> None:
        super().__init__()
        self._clock = clock
        self._rng = rng
        self.quotas = quotas
        self.workdirs = workdirs
        self._profiles = profiles or {}
        self._usage = usage
        self.incidents: list[Incident] = []
        self._processes: dict[str, _Process] = {}
        self._typed: dict[str, str] = {}
        self._sessions: dict[str, tuple[AgentKind, str]] = {}  # session → agent, workdir
        self._cwd: dict[str, str] = {}
        self._background: set[asyncio.Task[None]] = set()
        self._records = 0

    # --- the pane host -------------------------------------------------------------

    async def open_pane(self, *, cwd: str, label: str | None = None) -> str:
        pane_id = await super().open_pane(cwd=cwd, label=label)
        self._cwd[pane_id] = cwd
        return pane_id

    async def send_text(self, pane_id: str, text: str) -> None:
        await super().send_text(pane_id, text)
        self._typed[pane_id] = text

    async def send_keys(self, pane_id: str, keys: Sequence[str]) -> None:
        await super().send_keys(pane_id, keys)
        command = self._typed.pop(pane_id, "")
        if list(keys) == ["Enter"] and command:
            self._start(self._launch(pane_id, command))

    async def send_prompt(self, pane_id: str, text: str) -> None:
        await super().send_prompt(pane_id, text)
        self._start(self._turn(pane_id))

    # --- agents --------------------------------------------------------------------

    def _start(self, work: Coroutine[None, None, None]) -> None:
        task = asyncio.get_running_loop().create_task(work)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def _launched(self, command: str, workdir: str) -> _Process | None:
        for agent, adapter in ADAPTERS.items():
            if agent not in self.quotas:
                continue
            if command == adapter.launch_command():
                self._records += 1
                session = f"{agent.value}-{self._records}"
                self._sessions[session] = (agent, workdir)
                return _Process(agent, session, workdir, fresh=True)
            for session, (owner, where) in self._sessions.items():
                if owner is agent and command == adapter.resume_command(session):
                    return _Process(agent, session, where, fresh=False)
        return None

    async def _launch(self, pane_id: str, command: str) -> None:
        process = self._launched(command, self._cwd[pane_id])
        if process is None:
            return
        await asyncio.sleep(self._draw(self._profile(process.agent).launch_s))
        self._processes[pane_id] = process
        self._show(pane_id, "", working=False)

    async def _turn(self, pane_id: str) -> None:
        process = self._processes[pane_id]
        workdir = self.workdirs[process.workdir]
        profile = self._profile(process.agent)
        self._show(pane_id, "● Reading the task.", working=True)
        if process.fresh:
            share = profile.read_task_steps + profile.read_done_share * workdir.done
        else:
            share = profile.resume_share
        if not await self._respond(pane_id, process, share=share, overhead=True):
            return
        process.fresh = False
        while workdir.done < workdir.steps:
            step = workdir.done
            workdir.started += 1
            self._show(pane_id, f"● Step {step + 1} of {workdir.steps}.", working=True)
            fault = workdir.fault_at(step)
            if fault is not None and fault.kind != "slow":
                await self._fail(pane_id, process, fault)
                return
            took = fault.seconds if fault is not None else None
            if not await self._respond(pane_id, process, share=1.0, overhead=False, took=took):
                return
            workdir.done += 1
        self._show(pane_id, "● Done. All tests pass.", working=False)

    async def _respond(
        self,
        pane_id: str,
        process: _Process,
        *,
        share: float,
        overhead: bool,
        took: float | None = None,
    ) -> bool:
        """One response: time passes, tokens are spent. False if the limit cut it short."""
        workdir = self.workdirs[process.workdir]
        seconds, tokens = self._response(process.agent, share)
        seconds = took if took is not None else seconds
        counted = tokens.input + tokens.cache_write + tokens.output
        quota = self.quotas[process.agent]
        room = quota.room(self._clock.now())
        if counted > room:
            # The response runs until the window is used up, then the limit shows; with
            # nothing left, the agent answers the prompt with it at once.
            await asyncio.sleep(max(LIMIT_REPLY_S, seconds * max(room, 0) / counted))
            await self._spend(process, quota, max(room, 0), None, workdir, lost=True)
            now = self._clock.now()
            # The window is used up now, so it has an end; the fallback is never taken.
            self._limit(pane_id, process, quota.limited_until(now) or now + WINDOW)
            return False
        await asyncio.sleep(seconds)
        await self._spend(process, quota, counted, tokens, workdir, overhead=overhead)
        return True

    async def _spend(  # noqa: PLR0913 - one response's cost, and what it was for
        self,
        process: _Process,
        quota: Quota,
        counted: int,
        tokens: UsageTokens | None,
        workdir: Workdir,
        *,
        overhead: bool = False,
        lost: bool = False,
        logged: bool = True,
    ) -> None:
        now = self._clock.now()
        quota.spend(now, counted)
        workdir.tokens += counted
        if lost:
            workdir.wasted += counted
        elif overhead:
            workdir.overhead += counted
        if self._usage is None or counted == 0 or not logged:
            return
        if tokens is None:  # a response cut short: what it got to spend
            tokens = UsageTokens(input=counted, output=0, cache_read=0, cache_write=0)
        self._records += 1
        events: list[UsageRecord | RateLimitObservation] = [
            UsageRecord(
                agent=process.agent.value,  # type: ignore[arg-type]
                session_id=process.session,
                record_id=f"r{self._records}",
                at=now,
                tokens=tokens,
            )
        ]
        if process.agent is AgentKind.CODEX and quota.start is not None:
            events.append(
                RateLimitObservation(
                    agent="codex",
                    session_id=process.session,
                    at=now,
                    windows=(
                        RateLimitWindow(
                            name="primary",
                            window_minutes=300,
                            used_percent=min(100.0, 100.0 * quota.used / quota.cap),
                            resets_at=quota.start + WINDOW,
                        ),
                    ),
                )
            )
        await self._usage.record(events)  # the collector, without its few seconds' delay

    def _response(self, agent: AgentKind, share: float) -> tuple[float, UsageTokens]:
        """How long a response takes and what it uses: ``share`` of a step's."""
        profile = self._profile(agent)
        seconds = self._draw(profile.step_s) * share
        tokens = UsageTokens(
            input=round(self._draw(profile.input) * share),
            output=round(self._draw(profile.output) * share),
            cache_read=round(self._draw(profile.cache_read) * share),
            cache_write=round(self._draw(profile.cache_write) * share),
        )
        return seconds, tokens

    async def _fail(self, pane_id: str, process: _Process, fault: Fault) -> None:
        """A crash or a hang partway through a step; the step is lost either way."""
        seconds, tokens = self._response(process.agent, self._rng.uniform(0.2, 0.8))
        await asyncio.sleep(seconds)
        counted = tokens.input + tokens.cache_write + tokens.output
        quota = self.quotas[process.agent]
        # A frozen response never completes, so the agent never logs it: no usage record.
        await self._spend(
            process,
            quota,
            counted,
            tokens,
            self.workdirs[process.workdir],
            lost=True,
            logged=fault.kind != "hang",
        )
        self.incidents.append(
            Incident(fault.kind, process.workdir, process.agent, self._clock.now())
        )
        if fault.kind == "hang":
            process.frozen = True  # the screen keeps saying it works; nothing moves
            return
        del self._processes[pane_id]
        self.screens[pane_id] = "$ \n"
        self.set_observation(
            PaneObservation(
                pane_id=pane_id,
                agent=None,
                state=AgentState.UNKNOWN,
                evidence="herdr:no-agent",
                cwd=process.workdir,
            )
        )

    def _limit(self, pane_id: str, process: _Process, resets_at: datetime) -> None:
        now = self._clock.now()
        self.incidents.append(
            Incident("limit", process.workdir, process.agent, now, resets_at=resets_at)
        )
        self._show(pane_id, _limit_text(process.agent, resets_at), working=False)

    def _show(self, pane_id: str, body: str, *, working: bool) -> None:
        process = self._processes[pane_id]
        screen, state, evidence = _screen(process.agent, body, working=working)
        self.screens[pane_id] = screen
        self.set_observation(
            PaneObservation(
                pane_id=pane_id,
                agent=process.agent,
                state=state,
                evidence=evidence,
                session_ref=process.session,
                cwd=process.workdir,
            )
        )

    def _profile(self, agent: AgentKind) -> Profile:
        return self._profiles.get(agent, Profile())

    def _draw(self, bounds: tuple[float, float]) -> float:
        return self._rng.uniform(*bounds)
