"""Carry one task through attempts on one or more agents (roadmap slice 0).

The runner is the only place where decisions turn into actions. Everything it
learns or does is appended to the event log first; the board and the budget
are always folded from that log, so a run can be inspected and replayed.

For one task it:

1. picks the first preferred agent that has an adapter and is not limited;
2. starts an attempt, opens a pane in baton's workspace and launches the agent;
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
from dataclasses import dataclass, field, replace
from datetime import timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

from baton_herdr.budget.availability import fold_availability
from baton_herdr.core.commands import complete_task, start_attempt
from baton_herdr.core.contract import (
    DENIED_EVIDENCE,
    END_CONTRACT,
    QUESTION_EVIDENCE,
    EndStatus,
    one_line,
    parse_end,
)
from baton_herdr.core.detection import DetectionRequest
from baton_herdr.core.events import (
    AgentStateObserved,
    AttemptEnded,
    AttemptInterrupted,
    AttemptLocated,
    AttemptPrompted,
    AttemptResumed,
    Event,
    StoredEvent,
)
from baton_herdr.core.logging import get_logger, log_context
from baton_herdr.core.model import (
    AgentKind,
    AgentState,
    AttemptOutcome,
    AttemptStatus,
    InterruptReason,
    ObservationSource,
    TaskStatus,
)
from baton_herdr.core.notify import Notice, NoticeKind
from baton_herdr.core.panes import PaneHostError, PaneNotFoundError
from baton_herdr.core.projection import project
from baton_herdr.core.targeting import resolve_target
from baton_herdr.recovery.policy import Plan, plan_recovery
from baton_herdr.scheduler.operator import allowed_actions, find_blocker
from baton_herdr.scheduler.turn import Action, Step, TurnState, next_step, turn_from_log

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    from datetime import datetime

    from baton_herdr.core.agents import AgentAdapter
    from baton_herdr.core.model import AttemptId, TaskId
    from baton_herdr.core.notify import Notifier
    from baton_herdr.core.panes import PaneHost, PaneObservation
    from baton_herdr.core.ports import Clock, EventStore
    from baton_herdr.core.projection import AttemptView, Board, TaskView

CONTINUE_NOTE = (
    "Your session was interrupted. Continue the task from where you stopped; the working "
    "directory has your changes so far."
)

HANDOFF_NOTE = (
    "Another coding agent session started this task and stopped before finishing it. Its "
    "changes, if any, are in the working directory. Continue the task from there."
)

type PromptKind = Literal["task", "handoff", "continue"]

DENIED_NOTE = (
    "The permission was denied and the agent stopped. Reply with what it should do instead."
)

# Evidence attached to an observation where the agent refused to reopen the session.
RESUME_FAILED_EVIDENCE = "baton:resume-failed"


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
    # Automatic resumes after crashes and stalls, per task (ADR 0005).
    max_failure_resumes: int = 2


@dataclass(frozen=True, slots=True)
class _Context:
    task_id: TaskId
    attempt_id: AttemptId
    agent: AgentKind
    prompt: str
    prompt_kind: PromptKind
    pane_id: str | None  # verified to host the attempt, or None
    # False when the same blocker was already reported before a re-attach.
    announce: bool = True
    # The agent's own words about why it stopped (its end mark), if any.
    detail: str = ""


class _Outcome(StrEnum):
    FINISHED = "finished"
    HANDED_OFF = "handed_off"
    RESTARTED = "restarted"  # same task, fresh session; the same agent may run it
    INTERRUPTED = "interrupted"  # recorded; plan_recovery decides what follows
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
        self._log = get_logger("baton.scheduler")

    async def run(self, task_id: TaskId) -> TaskStatus:
        """Drive ``task_id`` until it is done, has to wait, or needs a person.

        Each pass looks at the task as the event log has it now: without a live
        attempt a new one is started on the first available agent; an interrupted
        attempt goes through ``plan_recovery``; an active one that nothing in this
        process drives (batond restarted, or it waited for a person) is re-attached.
        """
        with log_context(task_id=task_id):
            tried: set[AgentKind] = set()
            previous: AgentKind | None = None
            while True:
                task = (await self._board()).tasks[task_id]
                live = task.live_attempt
                if task.status.is_terminal:
                    break
                if live is None:
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
                    previous = agent
                elif live.status is AttemptStatus.INTERRUPTED:
                    outcome = await self._recover(task, live, tried)
                    if outcome in {_Outcome.HANDED_OFF, _Outcome.RESTARTED}:
                        previous = live.agent
                        if outcome is _Outcome.HANDED_OFF:
                            tried.add(live.agent)
                        else:
                            # A fresh session on the same agent is the point of a restart.
                            # Each restart costs a failure resume, so this cannot loop.
                            tried.discard(live.agent)
                        continue
                else:
                    outcome = await self._reattach(task, live)
                if outcome is not _Outcome.INTERRUPTED:
                    break
            return (await self._board()).tasks[task_id].status

    async def _recover(
        self, task: TaskView, attempt: AttemptView, tried: set[AgentKind]
    ) -> _Outcome:
        """Act on ``plan_recovery`` for an interrupted attempt."""
        events = await self._store.read()
        availability = fold_availability(events, cooldown=self._settings.limit_cooldown)
        now = self._clock.now()
        reason = attempt.interrupt_reason or InterruptReason.STALLED
        plan = plan_recovery(
            reason,
            has_session=attempt.session_ref is not None and attempt.agent in self._adapters,
            agent_available=availability.is_available(attempt.agent, now),
            another_agent_available=await self._pick_agent(tried | {attempt.agent}) is not None,
            failure_resumes_used=_failure_resumes(events, task.task_id),
            max_failure_resumes=self._settings.max_failure_resumes,
        )
        self._log.info("recovery plan", plan=plan.value, reason=reason.value)
        if plan is Plan.RESUME and attempt.session_ref is not None:
            return await self._resume(task, attempt, attempt.session_ref)
        if plan in {Plan.HAND_OFF, Plan.RESTART}:
            await self._append(
                AttemptEnded(
                    occurred_at=now,
                    task_id=task.task_id,
                    attempt_id=attempt.attempt_id,
                    outcome=AttemptOutcome.ABANDONED,
                )
            )
            return _Outcome.HANDED_OFF if plan is Plan.HAND_OFF else _Outcome.RESTARTED
        if plan is Plan.WAIT:
            until = availability.limited_until.get(attempt.agent)
            when = f" after {until:%Y-%m-%d %H:%M} UTC" if until else " later"
            await self._notify(
                NoticeKind.WAITING_FOR_AGENT,
                task.task_id,
                f"Every agent is limited; {attempt.agent.value} resumes this session{when}.",
                attempt.attempt_id,
            )
            return _Outcome.STOPPED
        await self._notify(
            NoticeKind.NEEDS_HUMAN,
            task.task_id,
            f"{attempt.agent.value} stopped ({reason.value}) and will not be resumed "
            "automatically. A person needs to look at it.",
            attempt.attempt_id,
        )
        return _Outcome.STOPPED

    async def _resume(self, task: TaskView, attempt: AttemptView, session_ref: str) -> _Outcome:
        """Continue the attempt's own agent session in a fresh pane (ADR 0006)."""
        adapter = self._adapters[attempt.agent]
        with log_context(attempt_id=str(attempt.attempt_id)):
            pane_id = await self._host.open_pane(cwd=task.workdir, label=task.title)
            now = self._clock.now()
            await self._append(
                AttemptResumed(
                    occurred_at=now, task_id=task.task_id, attempt_id=attempt.attempt_id
                ),
                AttemptLocated(
                    occurred_at=now,
                    task_id=task.task_id,
                    attempt_id=attempt.attempt_id,
                    pane_id=pane_id,
                ),
            )
            await self._host.send_text(pane_id, adapter.resume_command(session_ref))
            await self._host.send_keys(pane_id, ["Enter"])
            await self._notify(
                NoticeKind.TASK_RESUMED,
                task.task_id,
                f"Resuming the {attempt.agent.value} session.",
                attempt.attempt_id,
            )
            return await self._supervise(
                task.task_id,
                attempt.attempt_id,
                attempt.agent,
                pane_id,
                (_prompt(task, "continue"), "continue"),
            )

    async def _reattach(self, task: TaskView, attempt: AttemptView) -> _Outcome:
        """Supervise an active attempt from where the event log says its turn stands.

        Nothing is sent again that the log records as sent; the pane is verified
        before any input, as always (ADR 0006).
        """
        with log_context(attempt_id=str(attempt.attempt_id)):
            if attempt.agent not in self._adapters:
                await self._notify(
                    NoticeKind.NEEDS_HUMAN,
                    task.task_id,
                    f"No adapter for {attempt.agent.value}; this attempt cannot be supervised.",
                    attempt.attempt_id,
                )
                return _Outcome.STOPPED
            await self._locate(task.task_id, attempt.attempt_id)  # follows a moved session
            current = (await self._board()).tasks[task.task_id].live_attempt or attempt
            pane_id = current.pane_id
            if pane_id is None or not await self._pane_exists(pane_id):
                return await self._interrupt(
                    task.task_id,
                    attempt.attempt_id,
                    InterruptReason.CRASHED,
                    f"The {attempt.agent.value} pane is gone.",
                )
            events = await self._store.read()
            last = _last_observation(events, attempt.attempt_id)
            kind = _prompt_kind(events, task, attempt.attempt_id)
            turn = turn_from_log(events, attempt.attempt_id)
            if turn.prompt_sent:
                # batond may have been down for the whole turn, so its work was never
                # observed; an idle agent after a sent prompt has finished its turn.
                turn = replace(turn, worked=True)
            self._log.info("re-attaching", pane_id=pane_id, prompt_sent=turn.prompt_sent)
            return await self._supervise(
                task.task_id,
                attempt.attempt_id,
                attempt.agent,
                pane_id,
                (_prompt(task, kind), kind),
                turn=turn,
                last_seen=last,
            )

    async def _pane_exists(self, pane_id: str) -> bool:
        try:
            await self._host.observe(pane_id)
        except PaneNotFoundError:
            return False
        return True

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
            elif previous is agent:
                await self._notify(
                    NoticeKind.TASK_RESTARTED,
                    task_id,
                    f"Restarted on {agent.value} in a fresh session.",
                    attempt_id,
                )
            else:
                await self._notify(
                    NoticeKind.TASK_HANDED_OFF,
                    task_id,
                    f"Moved from {previous.value} to {agent.value}.",
                    attempt_id,
                )
            kind: PromptKind = "task" if previous is None else "handoff"
            return await self._supervise(
                task_id, attempt_id, agent, pane_id, (_prompt(task, kind), kind)
            )

    async def _supervise(  # noqa: PLR0913 - the attempt, plus where a re-attach resumes
        self,
        task_id: TaskId,
        attempt_id: AttemptId,
        agent: AgentKind,
        pane_id: str,
        prompt: tuple[str, PromptKind],
        *,
        turn: TurnState | None = None,
        last_seen: AgentStateObserved | None = None,
    ) -> _Outcome:
        """Watch the attempt until its turn ends or something else has to decide.

        ``turn`` and ``last_seen`` continue a turn already under way (a re-attach);
        by default a new turn starts.
        """
        adapter = self._adapters[agent]
        feed = _ObservationFeed(self._host, pane_id, self._settings.poll_interval_s)
        loop = asyncio.get_running_loop()
        turn = turn or TurnState()
        timeout = (
            self._settings.turn_timeout_s if turn.prompt_sent else self._settings.start_timeout_s
        )
        deadline = loop.time() + timeout
        last_state = last_seen.state if last_seen else None
        reported = (last_seen.state, last_seen.evidence) if last_seen else None
        saw_agent = last_seen is not None
        try:
            while True:
                if loop.time() > deadline:
                    phase = "work" if turn.prompt_sent else "start"
                    return await self._interrupt(
                        task_id, attempt_id, InterruptReason.STALLED, f"No progress during {phase}."
                    )
                try:
                    observation = await feed.next()
                except PaneNotFoundError:
                    observation = None
                state, evidence, resets_at, screen = await self._classify(
                    adapter, observation, pane_id, saw_agent=saw_agent
                )
                state, evidence, detail = _read_end_mark(state, evidence, screen, turn)
                if state is AgentState.BLOCKED_PERMISSION:
                    detail = adapter.permission_summary(screen) or ""
                saw_agent = saw_agent or (observation is not None and observation.agent is not None)
                if not saw_agent and state is not AgentState.CRASHED:
                    continue  # the agent process has not started yet
                if state is not last_state:
                    await self._observed(task_id, attempt_id, state, evidence)
                    last_state = state
                pane_for_input = await self._locate(task_id, attempt_id)
                startup_keys = (
                    adapter.startup_answer(evidence) if state is AgentState.BLOCKED_OTHER else None
                )
                turn, step = next_step(
                    turn, state, startup_keys=startup_keys, can_send=pane_for_input is not None
                )
                if step.action is Action.WAIT:
                    continue
                if step.action is Action.SEND_PROMPT:
                    deadline = loop.time() + self._settings.turn_timeout_s
                outcome = await self._carry_out(
                    step,
                    _Context(
                        task_id,
                        attempt_id,
                        agent,
                        *prompt,
                        pane_for_input,
                        announce=(state, evidence) != reported,
                        detail=detail,
                    ),
                    state=state,
                    evidence=evidence,
                    resets_at=resets_at,
                )
                if outcome is not None:
                    return outcome
        finally:
            await feed.aclose()

    async def _carry_out(
        self,
        step: Step,
        ctx: _Context,
        *,
        state: AgentState,
        evidence: str,
        resets_at: datetime | None,
    ) -> _Outcome | None:
        """Perform ``step``; return an outcome when the attempt is over for now."""
        if step.action is Action.SEND_PROMPT and ctx.pane_id is not None:
            # Recorded first: after a crash of batond a prompt is never sent twice.
            await self._append(
                AttemptPrompted(
                    occurred_at=self._clock.now(),
                    task_id=ctx.task_id,
                    attempt_id=ctx.attempt_id,
                    kind=ctx.prompt_kind,
                    chars=len(ctx.prompt),
                )
            )
            await self._host.send_prompt(ctx.pane_id, ctx.prompt)
            return None
        if step.action is Action.ANSWER_STARTUP and ctx.pane_id is not None:
            self._log.info("answering start-up dialog", evidence=evidence)
            await self._host.send_keys(ctx.pane_id, step.keys)
            return None
        if step.action is Action.FINISH:
            await self._finish(ctx.task_id)
            return _Outcome.FINISHED
        if step.action is Action.ASK_HUMAN:
            if ctx.announce:
                if not ctx.detail:
                    text = (
                        f"{ctx.agent.value} is waiting for a decision ({state.value}, {evidence})."
                    )
                elif state is AgentState.BLOCKED_PERMISSION:
                    text = f"{ctx.agent.value} asks for permission: {ctx.detail}"
                else:
                    text = f"{ctx.agent.value} is waiting for a decision: {ctx.detail}"
                blocker = find_blocker(await self._store.read(), ctx.task_id)
                actions = allowed_actions(blocker, self._adapters[ctx.agent]) if blocker else ()
                await self._deliver(
                    Notice(
                        NoticeKind.NEEDS_HUMAN,
                        ctx.task_id,
                        text,
                        ctx.attempt_id,
                        blocker_seq=blocker.seq if blocker else None,
                        actions=actions,
                    )
                )
            else:
                self._log.info("still waiting for a person", evidence=evidence)
            return _Outcome.STOPPED
        if step.action is Action.INTERRUPT and step.reason is not None:
            reason = (
                InterruptReason.RESUME_FAILED if evidence == RESUME_FAILED_EVIDENCE else step.reason
            )
            return await self._interrupt(
                ctx.task_id,
                ctx.attempt_id,
                reason,
                f"{ctx.agent.value} stopped: {reason.value}.",
                resets_at=resets_at,
            )
        return await self._interrupt(
            ctx.task_id,
            ctx.attempt_id,
            InterruptReason.STALLED,
            "The pane no longer hosts this attempt's agent.",
        )

    async def _classify(
        self,
        adapter: AgentAdapter,
        observation: PaneObservation | None,
        pane_id: str,
        *,
        saw_agent: bool,
    ) -> tuple[AgentState, str, datetime | None, str]:
        """The attempt's state, the evidence for it, a limit's reset time, and the screen."""
        if observation is None or observation.agent is None:
            # A resume the agent refused ends before the agent is ever seen running.
            with suppress(PaneHostError):
                screen = await self._host.read_screen(pane_id, lines=self._settings.screen_lines)
                if adapter.resume_failed(screen):
                    return AgentState.CRASHED, RESUME_FAILED_EVIDENCE, None, screen
            if saw_agent:
                return AgentState.CRASHED, "baton:agent-process-gone", None, ""
            return AgentState.UNKNOWN, "baton:agent-not-started", None, ""
        try:
            screen = await self._host.read_screen(pane_id, lines=self._settings.screen_lines)
        except PaneNotFoundError:
            return AgentState.CRASHED, "baton:pane-gone", None, ""
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
        return result.state, result.evidence, result.resets_at, screen

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
            ObservationSource.DETECTOR if evidence.startswith("baton:") else ObservationSource.HERDR
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
        """Record the interruption; what happens next is ``plan_recovery``'s call."""
        await self._append(
            AttemptInterrupted(
                occurred_at=self._clock.now(),
                task_id=task_id,
                attempt_id=attempt_id,
                reason=reason,
                resume_not_before=resets_at,
            )
        )
        kind = (
            NoticeKind.AGENT_LIMITED
            if reason is InterruptReason.RATE_LIMITED
            else NoticeKind.TASK_STOPPED
        )
        until = f" Available again at {resets_at:%Y-%m-%d %H:%M} UTC." if resets_at else ""
        await self._notify(kind, task_id, text + until, attempt_id)
        return _Outcome.INTERRUPTED

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
        await self._deliver(Notice(kind, task_id, text, attempt_id))

    async def _deliver(self, notice: Notice) -> None:
        self._log.info("notice", kind=notice.kind.value, text=notice.text)
        await self._notifier.notify(notice)

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


def _failure_resumes(events: Sequence[StoredEvent], task_id: TaskId) -> int:
    """How often this task was resumed after a crash or a stall."""
    count = 0
    last_reason: dict[AttemptId, InterruptReason] = {}
    for stored in events:
        event = stored.event
        if event.task_id != task_id:
            continue
        if isinstance(event, AttemptInterrupted):
            last_reason[event.attempt_id] = event.reason
        elif isinstance(event, AttemptResumed) and last_reason.get(event.attempt_id) in {
            InterruptReason.CRASHED,
            InterruptReason.STALLED,
        }:
            count += 1
    return count


def _prompt(task: TaskView, kind: PromptKind) -> str:
    if kind == "continue":
        body = CONTINUE_NOTE
    elif kind == "handoff":
        body = f"{HANDOFF_NOTE} {task.instructions}"
    else:
        body = task.instructions
    return one_line(f"{body} {END_CONTRACT}")


_END_STATES = {
    EndStatus.QUESTION: AgentState.BLOCKED_QUESTION,
    EndStatus.BLOCKED: AgentState.BLOCKED_OTHER,
}


def _read_end_mark(
    state: AgentState, evidence: str, screen: str, turn: TurnState
) -> tuple[AgentState, str, str]:
    """Refine a finished turn with the agent's end mark: a question is not a done task.

    Only an idle agent that has worked on the prompt is read, so a mark left on
    screen by an earlier turn (a resumed session) cannot end this one. Returns
    the state, its evidence, and the agent's words above the mark.
    """
    if state is not AgentState.IDLE or not (turn.prompt_sent and turn.worked):
        return state, evidence, ""
    mark = parse_end(screen)
    if mark is None and turn.denied:
        # A denied permission cuts the turn short without a mark; the agent waits
        # to be told what to do instead, which is not a finished task.
        return AgentState.BLOCKED_QUESTION, DENIED_EVIDENCE, DENIED_NOTE
    if mark is None or mark.status is EndStatus.DONE:
        return state, evidence, ""
    evidence = QUESTION_EVIDENCE if mark.status is EndStatus.QUESTION else "baton:end:blocked"
    return _END_STATES[mark.status], evidence, mark.text


def _prompt_kind(
    events: Iterable[StoredEvent], task: TaskView, attempt_id: AttemptId
) -> PromptKind:
    """What the attempt's current turn was (or will be) prompted with."""
    resumed = False
    for stored in events:
        event = stored.event
        if isinstance(event, AttemptResumed) and event.attempt_id == attempt_id:
            resumed = True
    if resumed:
        return "continue"
    return "task" if len(task.attempts) == 1 else "handoff"


def _last_observation(
    events: Iterable[StoredEvent], attempt_id: AttemptId
) -> AgentStateObserved | None:
    """The attempt's newest observation in its current turn, if any."""
    last: AgentStateObserved | None = None
    for stored in events:
        event = stored.event
        if getattr(event, "attempt_id", None) != attempt_id:
            continue
        if isinstance(event, AttemptResumed):
            last = None
        elif isinstance(event, AgentStateObserved):
            last = event
    return last
