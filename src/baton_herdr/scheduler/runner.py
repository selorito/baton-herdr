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
from zoneinfo import ZoneInfo

from baton_herdr.budget.availability import fold_availability
from baton_herdr.budget.choice import choose_agent
from baton_herdr.budget.load import BudgetReader
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
    AgentChosen,
    AgentStateObserved,
    AttemptEnded,
    AttemptInterrupted,
    AttemptLocated,
    AttemptPrompted,
    AttemptResumed,
    AttemptStarted,
    Event,
    PermissionDecided,
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
    OperatorAction,
    PermissionDecision,
    TaskStatus,
)
from baton_herdr.core.panes import PaneHostError, PaneNotFoundError
from baton_herdr.core.projection import project
from baton_herdr.core.redact import redact
from baton_herdr.core.targeting import resolve_target
from baton_herdr.policy.rules import Verdict
from baton_herdr.recovery.policy import Plan, plan_recovery
from baton_herdr.scheduler import notices
from baton_herdr.scheduler.operator import (
    ActionRefusedError,
    act,
    allowed_actions,
    answered,
    find_blocker,
)
from baton_herdr.scheduler.stall import ProgressWatch
from baton_herdr.scheduler.turn import Action, Step, TurnState, next_step, turn_from_log

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    from datetime import datetime

    from baton_herdr.core.agents import AgentAdapter
    from baton_herdr.core.detector import Detector
    from baton_herdr.core.model import AttemptId, TaskId
    from baton_herdr.core.notify import Notice, Notifier
    from baton_herdr.core.panes import PaneHost, PaneObservation
    from baton_herdr.core.ports import Clock, EventStore, Workspace
    from baton_herdr.core.projection import AttemptView, Board, TaskView
    from baton_herdr.policy.load import Policy
    from baton_herdr.scheduler.operator import Blocker

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

# What the runner does about a permission prompt the policy decided (ADR 0013).
_POLICY_ACTIONS = {
    PermissionDecision.ALLOW: OperatorAction.APPROVE,
    PermissionDecision.DENY: OperatorAction.DENY,
}
POLICY_ACTOR = "policy"

# Evidence attached to an observation where the agent refused to reopen the session.
RESUME_FAILED_EVIDENCE = "baton:resume-failed"


@dataclass(frozen=True, slots=True)
class RunnerSettings:
    agents: tuple[AgentKind, ...] = (AgentKind.CLAUDE, AgentKind.CODEX)
    limit_cooldown: timedelta = field(default_factory=lambda: timedelta(hours=1))
    start_timeout_s: float = 120
    turn_timeout_s: float = 3600
    poll_interval_s: float = 2
    # No change on screen and no tokens for this long while working: stalled (ADR 0014).
    stall_after: timedelta = field(default_factory=lambda: timedelta(minutes=15))
    stall_after_without_usage: timedelta = field(default_factory=lambda: timedelta(minutes=30))
    timezone: str = "UTC"
    screen_lines: int = 80
    # Overrides of the adapters' launch commands, per agent.
    launch_commands: Mapping[AgentKind, str] = field(default_factory=dict)
    # Automatic resumes after crashes and stalls, per task (ADR 0005).
    max_failure_resumes: int = 2
    # An agent with less budget left than this goes behind the others (ADR 0012).
    reserve_percent: float = 10
    # Before a task's tokens are summed, time for the usage collector to store the
    # agent's last response (it reads the logs every couple of seconds).
    usage_grace_s: float = 3


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
    # The screen the decision was made on, for notices.
    screen: str = ""


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
        detector: Detector,
        budget: BudgetReader | None = None,
        workspace: Workspace | None = None,
        policy: Policy | None = None,
    ) -> None:
        self._store = store
        self._host = host
        self._adapters = adapters
        self._notifier = notifier
        self._clock = clock
        self._settings = settings or RunnerSettings()
        # Tells what a task changed in its directory; without it, notices leave that out.
        self._workspace = workspace
        # Classifies screens: baton-detect in batond (ADR 0011).
        self._detector = detector
        # Decides permission prompts it can read (ADR 0013); without it, a person decides all.
        self._policy = policy
        # Without usage data the budget is folded from the event log alone.
        self._budget = budget or BudgetReader(usage=None, cooldown=self._settings.limit_cooldown)
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
                    agent, reason = await self._choose_agent(task_id, tried)
                    if agent is None:
                        await self._deliver(notices.no_agent(task, reason))
                        break
                    tried.add(agent)
                    outcome = await self._attempt(task_id, agent, previous, reason)
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
            await self._deliver(
                notices.waiting_for_reset(
                    task,
                    attempt.agent,
                    availability.limited_until.get(attempt.agent),
                    now=now,
                    zone=self._zone,
                    attempt_id=attempt.attempt_id,
                )
            )
            return _Outcome.STOPPED
        await self._deliver(
            notices.needs_human(
                task,
                f"{attempt.agent.value} {notices.STOPPED[reason]} and will not be resumed "
                "automatically. A person needs to look at it.",
                attempt_id=attempt.attempt_id,
            )
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
            await self._deliver(notices.resumed(task, attempt.agent, attempt.attempt_id))
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
                await self._deliver(
                    notices.needs_human(
                        task,
                        f"No adapter for {attempt.agent.value}; this attempt cannot be supervised.",
                        attempt_id=attempt.attempt_id,
                    )
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

    async def _choose_agent(
        self, task_id: TaskId, tried: set[AgentKind]
    ) -> tuple[AgentKind | None, str]:
        """Pick the agent for the task's next attempt and record why (ADR 0012)."""
        events = await self._store.read()
        now = self._clock.now()
        agents = [agent for agent in self._settings.agents if agent in self._adapters]
        budgets = await self._budget.read(agents, events, now)
        choice = choose_agent(
            agents,
            budgets,
            reserve_percent=self._settings.reserve_percent,
            zone=ZoneInfo(self._settings.timezone),
            tried=tried,
        )
        self._log.info("agent chosen", agent=choice.agent, reason=choice.reason)
        await self._append(
            AgentChosen(
                occurred_at=now,
                task_id=task_id,
                agent=choice.agent,
                reason=choice.reason,
                budgets=choice.notes,
            )
        )
        return choice.agent, choice.reason

    async def _attempt(
        self, task_id: TaskId, agent: AgentKind, previous: AgentKind | None, reason: str
    ) -> _Outcome:
        events = await self._store.read()
        task = project(events).tasks[task_id]
        # Why the previous attempt stopped, for a hand-off notice; its view forgets once ended.
        before = _last_interruption(events, task.attempts[-1].attempt_id) if task.attempts else None
        head = await self._workspace.head(task.workdir) if self._workspace else None
        started = start_attempt(task, agent, at=self._clock.now(), workdir_head=head)
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
                notice = notices.started(task, agent, reason, attempt_id)
            elif previous is agent:
                notice = notices.restarted(task, agent, reason, attempt_id)
            else:
                notice = notices.handed_off(
                    task,
                    previous,
                    agent,
                    stopped=before.reason if before else None,
                    available_at=before.resume_not_before if before else None,
                    reason=reason,
                    now=self._clock.now(),
                    zone=self._zone,
                    attempt_id=attempt_id,
                )
            await self._deliver(notice)
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
        screen = ""
        progress = ProgressWatch()
        try:
            while True:
                if loop.time() > deadline:
                    phase = "work" if turn.prompt_sent else "start"
                    return await self._interrupt(
                        task_id,
                        attempt_id,
                        InterruptReason.STALLED,
                        f"No progress during {phase}.",
                        screen=screen,
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
                stuck = await self._no_progress(
                    progress, state, turn, screen, task_id=task_id, agent=agent
                )
                if stuck:
                    return await self._interrupt(
                        task_id, attempt_id, InterruptReason.STALLED, stuck, screen=screen
                    )
                pane_for_input = await self._locate(task_id, attempt_id)
                startup_keys = (
                    adapter.startup_answer(evidence) if state is AgentState.BLOCKED_OTHER else None
                )
                turn, step = next_step(
                    turn, state, startup_keys=startup_keys, can_send=pane_for_input is not None
                )
                if step.action is Action.WAIT:
                    continue
                if (
                    step.action is Action.ASK_HUMAN
                    and state is AgentState.BLOCKED_PERMISSION
                    and pane_for_input is not None
                    and (
                        applied := await self._apply_policy(
                            task_id, attempt_id, adapter, evidence, screen
                        )
                    )
                ):
                    # After a refusal the agent stops and waits to be told what to do instead.
                    turn = replace(turn, denied=turn.denied or applied is OperatorAction.DENY)
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
                        screen=screen,
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
            await self._finish(ctx.task_id, ctx.agent, ctx.attempt_id)
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
                events = await self._store.read()
                blocker = find_blocker(events, ctx.task_id)
                actions = allowed_actions(blocker, self._adapters[ctx.agent]) if blocker else ()
                await self._deliver(
                    notices.needs_human(
                        project(events).tasks[ctx.task_id],
                        text,
                        screen=ctx.screen,
                        attempt_id=ctx.attempt_id,
                        blocker_seq=blocker.seq if blocker else None,
                        actions=actions,
                        policy=_policy_note(events, ctx.attempt_id),
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
                ctx.task_id, ctx.attempt_id, reason, resets_at=resets_at, screen=ctx.screen
            )
        return await self._interrupt(
            ctx.task_id,
            ctx.attempt_id,
            InterruptReason.STALLED,
            "The pane no longer hosts this attempt's agent.",
        )

    async def _no_progress(  # noqa: PLR0913 - the watch, what is seen, and whose tokens
        self,
        watch: ProgressWatch,
        state: AgentState,
        turn: TurnState,
        screen: str,
        *,
        task_id: TaskId,
        agent: AgentKind,
    ) -> str:
        """Why a working agent counts as stuck, or "" while it makes progress (ADR 0014)."""
        now = self._clock.now()
        if state is not AgentState.WORKING or not turn.prompt_sent:
            watch.pause()
            return ""
        watch.working(screen, now)
        collecting = self._budget.usage is not None
        limit = (
            self._settings.stall_after if collecting else self._settings.stall_after_without_usage
        )
        if not watch.quiet(now, limit):
            return ""
        if collecting:
            latest = await self._latest_tokens(task_id, agent, since=now - limit)
            if latest is not None:
                watch.tokens(latest)
            if not watch.quiet(now, limit):
                return ""
        minutes = int((watch.quiet_for(now) or limit).total_seconds() // 60)
        if collecting:
            return (
                f"Working for {minutes} min with no change on screen and no tokens recorded "
                "for its session."
            )
        return f"Working for {minutes} min with no change on screen (usage is not collected)."

    async def _latest_tokens(
        self, task_id: TaskId, agent: AgentKind, *, since: datetime
    ) -> datetime | None:
        """When the attempt's session last had tokens recorded, from ``since`` on.

        Records without the session's id count for the agent as a whole, as in ``spent``.
        """
        if self._budget.usage is None:
            return None
        attempt = (await self._board()).tasks[task_id].live_attempt
        session = attempt.session_ref if attempt else None
        records = [r for r in await self._budget.usage.usage(since=since) if r.agent == agent.value]
        if session is not None and any(r.session_id == session for r in records):
            records = [r for r in records if r.session_id == session]
        return max((r.at for r in records), default=None)

    async def _apply_policy(
        self,
        task_id: TaskId,
        attempt_id: AttemptId,
        adapter: AgentAdapter,
        evidence: str,
        screen: str,
    ) -> OperatorAction | None:
        """Let the policy answer a permission prompt; ``None`` leaves it to a person.

        Each prompt is decided once, and the decision is recorded before anything is
        sent. The answer goes through the operator's own path (``act``), with its checks:
        the prompt still open, not answered yet, the pane verified.
        """
        if self._policy is None:
            return None
        command = adapter.permission_command(screen)
        prompt = adapter.permission_summary(screen)
        prompt = redact(prompt)[:1000] if prompt else None
        events, blocker, decided = await self._open_prompt(task_id, attempt_id, evidence, prompt)
        if blocker is None:
            return None
        if decided is not None:
            action = _POLICY_ACTIONS.get(decided.decision)
            # Already answered: wait for the screen to move on. Not answered (the pane
            # could not be verified): a person decides.
            return action if action and answered(events, blocker.seq) else None
        workdir = project(events).tasks[task_id].workdir
        verdict = self._policy.decide(adapter.kind, command, workdir)
        action = _POLICY_ACTIONS.get(verdict.decision)
        if action is not None and not adapter.permission_keys(
            evidence, approve=action is OperatorAction.APPROVE
        ):
            verdict = Verdict(
                PermissionDecision.ASK,
                f"{verdict.reason}; but baton does not know this prompt's keys",
                verdict.rule,
            )
            action = None
        await self._append(
            PermissionDecided(
                occurred_at=self._clock.now(),
                task_id=task_id,
                attempt_id=attempt_id,
                blocker_seq=blocker.seq,
                decision=verdict.decision,
                prompt=prompt,
                command=redact(command)[:1000] if command else None,
                rule=verdict.rule.label if verdict.rule else None,
                reason=redact(verdict.reason),
            )
        )
        self._log.info(
            "permission decided by policy",
            decision=verdict.decision.value,
            rule=verdict.rule.label if verdict.rule else None,
        )
        if action is None:
            return None
        try:
            await act(
                store=self._store,
                host=self._host,
                adapters=self._adapters,
                clock=self._clock,
                task_id=task_id,
                action=action,
                by=POLICY_ACTOR,
                blocker_seq=blocker.seq,
            )
        except ActionRefusedError as err:
            self._log.warning("policy decision not carried out", error=str(err))
            return None
        return action

    async def _open_prompt(
        self, task_id: TaskId, attempt_id: AttemptId, evidence: str, prompt: str | None
    ) -> tuple[Sequence[StoredEvent], Blocker | None, PermissionDecided | None]:
        """The permission prompt the attempt waits on, and the policy's decision on it so far.

        Another prompt on screen under an unchanged state is observed afresh, so it
        becomes a blocker of its own and is decided on its own. A screen that shows no
        prompt (herdr's state lags behind the answer) is the same blocker.
        """
        events = await self._store.read()
        blocker = find_blocker(events, task_id)
        if blocker is None or blocker.state is not AgentState.BLOCKED_PERMISSION:
            return events, None, None
        decided = _decision_for(events, blocker.seq)
        if decided is None or prompt is None or decided.prompt == prompt:
            return events, blocker, decided
        await self._observed(task_id, attempt_id, blocker.state, evidence)
        events = await self._store.read()
        return events, find_blocker(events, task_id), None

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
        result = await self._detector.classify(
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

    async def _interrupt(  # noqa: PLR0913 - the attempt, why it stopped, and what showed it
        self,
        task_id: TaskId,
        attempt_id: AttemptId,
        reason: InterruptReason,
        detail: str = "",
        *,
        resets_at: datetime | None = None,
        screen: str = "",
    ) -> _Outcome:
        """Record the interruption; what happens next is ``plan_recovery``'s call.

        A usage limit is not announced here: the notice of what follows (a hand-off,
        a wait for the reset) says it, with the reset time.
        """
        await self._append(
            AttemptInterrupted(
                occurred_at=self._clock.now(),
                task_id=task_id,
                attempt_id=attempt_id,
                reason=reason,
                resume_not_before=resets_at,
                detail=detail[:500] or None,
            )
        )
        if reason is not InterruptReason.RATE_LIMITED:
            task = (await self._board()).tasks[task_id]
            attempt = next(a for a in task.attempts if a.attempt_id == attempt_id)
            await self._deliver(
                notices.stopped(
                    task, attempt.agent, reason, detail=detail, screen=screen, attempt_id=attempt_id
                )
            )
        return _Outcome.INTERRUPTED

    async def _finish(self, task_id: TaskId, agent: AgentKind, attempt_id: AttemptId) -> None:
        now = self._clock.now()
        task = (await self._board()).tasks[task_id]
        await self._append(*complete_task(task, at=now))
        events = await self._store.read()
        starts = [
            e.event
            for e in events
            if isinstance(e.event, AttemptStarted) and e.event.task_id == task_id
        ]
        attempt_start = next(e.occurred_at for e in starts if e.attempt_id == attempt_id)
        session = next(a.session_ref for a in task.attempts if a.attempt_id == attempt_id)
        spent = budget = changes = None
        try:
            if self._budget.usage is not None and self._settings.usage_grace_s > 0:
                await asyncio.sleep(self._settings.usage_grace_s)
            spent = await self._budget.spent(agent, start=attempt_start, end=now, session=session)
            if self._budget.usage is not None:
                budget = (await self._budget.read([agent], events, now)).get(agent)
        except Exception as err:  # noqa: BLE001 - a notice without figures is still a notice
            self._log.warning("usage for the done notice failed", error=repr(err))
        if self._workspace is not None:
            changes = await self._workspace.changes(task.workdir, since=starts[0].workdir_head)
        await self._deliver(
            notices.completed(
                task,
                agent,
                took=now - starts[0].occurred_at,
                attempts=len(starts),
                spent=spent,
                budget=budget,
                changes=changes,
                attempt_id=attempt_id,
            )
        )

    async def _deliver(self, notice: Notice) -> None:
        self._log.info("notice", kind=notice.kind.value, text=notice.plain)
        await self._notifier.notify(notice)

    @property
    def _zone(self) -> ZoneInfo:
        return ZoneInfo(self._settings.timezone)

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


def _last_interruption(
    events: Iterable[StoredEvent], attempt_id: AttemptId
) -> AttemptInterrupted | None:
    last = None
    for stored in events:
        if isinstance(stored.event, AttemptInterrupted) and stored.event.attempt_id == attempt_id:
            last = stored.event
    return last


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


def _decision_for(events: Iterable[StoredEvent], blocker_seq: int) -> PermissionDecided | None:
    for stored in events:
        if isinstance(stored.event, PermissionDecided) and stored.event.blocker_seq == blocker_seq:
            return stored.event
    return None


def _policy_note(events: Iterable[StoredEvent], attempt_id: AttemptId) -> str:
    """What the policy said about the turn's newest prompt, if it left it to a person or refused."""
    note = ""
    for stored in events:
        event = stored.event
        if getattr(event, "attempt_id", None) != attempt_id:
            continue
        if isinstance(event, AttemptPrompted | AttemptResumed):
            note = ""
        elif isinstance(event, PermissionDecided):
            note = (
                ""
                if event.decision is PermissionDecision.ALLOW
                else f"Policy: {event.decision.value}, {event.reason}."
            )
    return note
