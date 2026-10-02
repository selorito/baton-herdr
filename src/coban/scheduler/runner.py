"""Carry one task through attempts on one or more agents (roadmap slice 0).

The runner is the only place where decisions turn into actions. Everything it
learns or does is appended to the event log first; the board and the budget
are always folded from that log, so a run can be inspected and replayed.

For one task it:

1. picks the first preferred agent that has an adapter and is not limited;
2. starts an attempt, opens a pane in coban's workspace and launches the agent;
3. waits for the agent's prompt, verifies the pane (ADR 0006) and sends the task;
4. classifies every observation with the agent's adapter and asks the recovery
   policy what it means;
5. on a usage limit records the interruption and hands the task to the next
   available agent; on a finished turn completes the task; on anything that needs
   a person it stops and says so.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum
from typing import TYPE_CHECKING

from coban.budget.availability import fold_availability
from coban.core.commands import complete_task, start_attempt
from coban.core.detection import DetectionRequest
from coban.core.events import (
    AgentStateObserved,
    AttemptEnded,
    AttemptInterrupted,
    AttemptLocated,
    Event,
)
from coban.core.logging import get_logger, log_context
from coban.core.model import (
    AgentKind,
    AgentState,
    AttemptOutcome,
    InterruptReason,
    ObservationSource,
    TaskStatus,
)
from coban.core.notify import Notice, NoticeKind
from coban.core.panes import PaneHostError, PaneNotFoundError
from coban.core.projection import project
from coban.core.targeting import resolve_target
from coban.recovery.policy import Verdict, assess, hands_off, interrupt_reason

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

    from coban.core.agents import AgentAdapter
    from coban.core.model import AttemptId, TaskId
    from coban.core.notify import Notifier
    from coban.core.panes import PaneHost, PaneObservation
    from coban.core.ports import Clock, EventStore
    from coban.core.projection import Board

HANDOFF_NOTE = (
    "Another coding agent started this task and stopped because it reached its usage "
    "limit. Its changes, if any, are in the working directory. Continue the task from "
    "there."
)


@dataclass(frozen=True, slots=True)
class RunnerSettings:
    agents: tuple[AgentKind, ...] = (AgentKind.CLAUDE, AgentKind.CODEX)
    limit_cooldown: timedelta = field(default_factory=lambda: timedelta(hours=1))
    start_timeout_s: float = 120
    turn_timeout_s: float = 3600
    poll_interval_s: float = 2
    timezone: str = "UTC"
    screen_lines: int = 80
    # Overrides of the adapters' launch commands, per agent.
    launch_commands: Mapping[AgentKind, str] = field(default_factory=dict)


class _Outcome(StrEnum):
    FINISHED = "finished"
    HANDED_OFF = "handed_off"
    STOPPED = "stopped"


class TaskRunner:
    def __init__(  # noqa: PLR0913 - every collaborator is a separate port
        self,
        *,
        store: EventStore,
        host: PaneHost,
        adapters: Mapping[AgentKind, AgentAdapter],
        notifier: Notifier,
        clock: Clock,
        settings: RunnerSettings | None = None,
    ) -> None:
        self._store = store
        self._host = host
        self._adapters = adapters
        self._notifier = notifier
        self._clock = clock
        self._settings = settings or RunnerSettings()
        self._log = get_logger("coban.scheduler")

    async def run(self, task_id: TaskId) -> TaskStatus:
        """Drive ``task_id`` until it is done, waits for an agent, or needs a person."""
        with log_context(task_id=task_id):
            task = (await self._board()).tasks[task_id]
            if task.status.is_terminal:
                return task.status
            if task.live_attempt is not None:
                await self._notify(
                    NoticeKind.TASK_STOPPED,
                    task_id,
                    "The task already has a live attempt; resuming one is not supported yet.",
                )
                return task.status

            tried: set[AgentKind] = set()
            previous: AgentKind | None = None
            while True:
                agent = await self._pick_agent(tried)
                if agent is None:
                    await self._notify(
                        NoticeKind.WAITING_FOR_AGENT,
                        task_id,
                        "No agent is available right now; the task waits.",
                    )
                    break
                tried.add(agent)
                outcome = await self._attempt(task_id, agent, previous)
                if outcome is not _Outcome.HANDED_OFF:
                    break
                previous = agent
            return (await self._board()).tasks[task_id].status

    async def _pick_agent(self, tried: set[AgentKind]) -> AgentKind | None:
        availability = fold_availability(
            await self._store.read(), cooldown=self._settings.limit_cooldown
        )
        candidates = [
            agent
            for agent in self._settings.agents
            if agent in self._adapters and agent not in tried
        ]
        available = availability.available(candidates, self._clock.now())
        return available[0] if available else None

    async def _attempt(
        self, task_id: TaskId, agent: AgentKind, previous: AgentKind | None
    ) -> _Outcome:
        task = (await self._board()).tasks[task_id]
        started = start_attempt(task, agent, at=self._clock.now())
        await self._append(started)
        attempt_id = started.attempt_id
        with log_context(attempt_id=str(attempt_id)):
            pane_id = await self._host.open_pane(cwd=task.workdir, label=task.title)
            await self._append(
                AttemptLocated(
                    occurred_at=self._clock.now(),
                    task_id=task_id,
                    attempt_id=attempt_id,
                    pane_id=pane_id,
                )
            )
            command = (
                self._settings.launch_commands.get(agent) or self._adapters[agent].launch_command()
            )
            await self._host.send_text(pane_id, command)
            await self._host.send_keys(pane_id, ["Enter"])
            if previous is None:
                await self._notify(
                    NoticeKind.TASK_STARTED, task_id, f"Started on {agent.value}.", attempt_id
                )
            else:
                await self._notify(
                    NoticeKind.TASK_HANDED_OFF,
                    task_id,
                    f"Moved from {previous.value} to {agent.value}.",
                    attempt_id,
                )
            prompt = (
                task.instructions if previous is None else f"{HANDOFF_NOTE}\n\n{task.instructions}"
            )
            return await self._supervise(task_id, attempt_id, agent, pane_id, prompt)

    async def _supervise(  # noqa: PLR0912 - one state machine, kept in one place
        self,
        task_id: TaskId,
        attempt_id: AttemptId,
        agent: AgentKind,
        pane_id: str,
        prompt: str,
    ) -> _Outcome:
        adapter = self._adapters[agent]
        feed = _ObservationFeed(self._host, pane_id, self._settings.poll_interval_s)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._settings.start_timeout_s
        last_state: AgentState | None = None
        saw_agent = prompt_sent = worked = False
        try:
            while True:
                if loop.time() > deadline:
                    phase = "work" if prompt_sent else "start"
                    return await self._interrupt(
                        task_id, attempt_id, InterruptReason.STALLED, f"No progress during {phase}."
                    )
                try:
                    observation = await feed.next()
                except PaneNotFoundError:
                    observation = None
                state, evidence, resets_at = await self._classify(
                    adapter, observation, pane_id, saw_agent=saw_agent
                )
                if observation is not None and observation.agent is not None:
                    saw_agent = True
                if not saw_agent and state is not AgentState.CRASHED:
                    continue  # the agent process has not started yet
                if state is not last_state:
                    await self._observed(task_id, attempt_id, state, evidence)
                    last_state = state
                pane_for_input = await self._locate(task_id, attempt_id)

                if not prompt_sent:
                    if state is AgentState.IDLE:
                        if pane_for_input is None:
                            return await self._interrupt(
                                task_id,
                                attempt_id,
                                InterruptReason.STALLED,
                                "The pane no longer hosts this attempt's agent.",
                            )
                        await self._host.send_prompt(pane_for_input, prompt)
                        prompt_sent = True
                        deadline = loop.time() + self._settings.turn_timeout_s
                        continue
                    verdict = assess(state, worked=False)
                    if verdict is Verdict.CONTINUE:
                        continue
                else:
                    worked = worked or state is AgentState.WORKING
                    verdict = assess(state, worked=worked)

                if verdict is Verdict.CONTINUE:
                    continue
                if verdict is Verdict.TURN_FINISHED:
                    await self._finish(task_id)
                    return _Outcome.FINISHED
                if verdict is Verdict.NEEDS_HUMAN:
                    await self._notify(
                        NoticeKind.NEEDS_HUMAN,
                        task_id,
                        f"{agent.value} is waiting for a decision ({state.value}, {evidence}).",
                        attempt_id,
                    )
                    return _Outcome.STOPPED
                reason = interrupt_reason(state)
                return await self._interrupt(
                    task_id,
                    attempt_id,
                    reason,
                    f"{agent.value} stopped: {reason.value}.",
                    resets_at=resets_at,
                )
        finally:
            await feed.aclose()

    async def _classify(
        self,
        adapter: AgentAdapter,
        observation: PaneObservation | None,
        pane_id: str,
        *,
        saw_agent: bool,
    ) -> tuple[AgentState, str, datetime | None]:
        if observation is None or observation.agent is None:
            if saw_agent:
                return AgentState.CRASHED, "coban:agent-process-gone", None
            return AgentState.UNKNOWN, "coban:agent-not-started", None
        try:
            screen = await self._host.read_screen(pane_id, lines=self._settings.screen_lines)
        except PaneNotFoundError:
            return AgentState.CRASHED, "coban:pane-gone", None
        result = adapter.classify(
            DetectionRequest(
                agent=adapter.kind,
                screen=screen,
                host_state=observation.state,
                host_evidence=observation.evidence,
                observed_at=self._clock.now(),
                timezone=self._settings.timezone,
            )
        )
        return result.state, result.evidence, result.resets_at

    async def _locate(self, task_id: TaskId, attempt_id: AttemptId) -> str | None:
        """Update the attempt's location and return a pane that may receive input."""
        attempt = (await self._board()).tasks[task_id].live_attempt
        if attempt is None or attempt.attempt_id != attempt_id:
            return None
        try:
            target = await resolve_target(self._host, task_id, attempt, at=self._clock.now())
        except PaneHostError:
            return None
        if target.events:
            await self._append(*target.events)
        return target.pane_id if target.check.may_send else None

    async def _observed(
        self, task_id: TaskId, attempt_id: AttemptId, state: AgentState, evidence: str
    ) -> None:
        source = (
            ObservationSource.DETECTOR if evidence.startswith("coban:") else ObservationSource.HERDR
        )
        await self._append(
            AgentStateObserved(
                occurred_at=self._clock.now(),
                task_id=task_id,
                attempt_id=attempt_id,
                state=state,
                source=source,
                evidence=evidence,
            )
        )

    async def _interrupt(
        self,
        task_id: TaskId,
        attempt_id: AttemptId,
        reason: InterruptReason,
        text: str,
        *,
        resets_at: datetime | None = None,
    ) -> _Outcome:
        now = self._clock.now()
        events: list[Event] = [
            AttemptInterrupted(
                occurred_at=now,
                task_id=task_id,
                attempt_id=attempt_id,
                reason=reason,
                resume_not_before=resets_at,
            )
        ]
        if hands_off(reason):
            events.append(
                AttemptEnded(
                    occurred_at=now,
                    task_id=task_id,
                    attempt_id=attempt_id,
                    outcome=AttemptOutcome.ABANDONED,
                )
            )
        await self._append(*events)
        kind = (
            NoticeKind.AGENT_LIMITED
            if reason is InterruptReason.RATE_LIMITED
            else NoticeKind.TASK_STOPPED
        )
        until = f" Available again at {resets_at:%Y-%m-%d %H:%M} UTC." if resets_at else ""
        await self._notify(kind, task_id, text + until, attempt_id)
        return _Outcome.HANDED_OFF if hands_off(reason) else _Outcome.STOPPED

    async def _finish(self, task_id: TaskId) -> None:
        task = (await self._board()).tasks[task_id]
        await self._append(*complete_task(task, at=self._clock.now()))
        await self._notify(NoticeKind.TASK_COMPLETED, task_id, f"Done: {task.title}")

    async def _notify(
        self,
        kind: NoticeKind,
        task_id: TaskId,
        text: str,
        attempt_id: AttemptId | None = None,
    ) -> None:
        self._log.info("notice", kind=kind.value, text=text)
        await self._notifier.notify(Notice(kind, task_id, text, attempt_id))

    async def _append(self, *events: Event) -> None:
        await self._store.append(events)

    async def _board(self) -> Board:
        return project(await self._store.read())


class _ObservationFeed:
    """herdr's change events, with a periodic read in between.

    Watching alone could miss a screen that changes without a status change (a
    limit message under an unchanged status); polling alone would be slow. Events
    are pumped into a queue so that waiting for one never cancels the watch.
    """

    def __init__(self, host: PaneHost, pane_id: str, poll_interval_s: float) -> None:
        self._host = host
        self._pane_id = pane_id
        self._poll_interval_s = poll_interval_s
        self._queue: asyncio.Queue[PaneObservation | BaseException] = asyncio.Queue()
        self._pump_task = asyncio.create_task(self._pump())

    async def _pump(self) -> None:
        try:
            async for observation in self._host.watch(self._pane_id):
                self._queue.put_nowait(observation)
        except PaneHostError as err:
            self._queue.put_nowait(err)

    async def next(self) -> PaneObservation:
        try:
            item = await asyncio.wait_for(self._queue.get(), self._poll_interval_s)
        except TimeoutError:
            return await self._host.observe(self._pane_id)
        if isinstance(item, BaseException):
            raise item
        return item

    async def aclose(self) -> None:
        self._pump_task.cancel()
        with suppress(asyncio.CancelledError):
            await self._pump_task
