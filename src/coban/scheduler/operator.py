"""Operator actions on a waiting attempt, from Telegram or the CLI (ADR 0009).

An action is bound to a *blocker*: the ``attempt.state_observed`` event that
recorded why the attempt waits for a person. It is carried out only while that
blocker is still the attempt's newest observation, only once, only if the
action fits the blocker, and only into a pane verified to host the attempt.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from coban.core.contract import ANSWERABLE_EVIDENCE, one_line
from coban.core.events import AgentStateObserved, OperatorActed
from coban.core.model import AgentState, AttemptStatus, OperatorAction
from coban.core.panes import PaneHostError
from coban.core.projection import project
from coban.core.targeting import resolve_target

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from coban.core.agents import AgentAdapter
    from coban.core.events import StoredEvent
    from coban.core.model import AgentKind, AttemptId, TaskId
    from coban.core.panes import PaneHost
    from coban.core.ports import Clock, EventStore


@dataclass(frozen=True, slots=True)
class Blocker:
    task_id: TaskId
    attempt_id: AttemptId
    agent: AgentKind
    seq: int
    state: AgentState
    evidence: str


class ActionRefusedError(Exception):
    """The action was not carried out; the message says why, for the operator."""


def find_blocker(events: Iterable[StoredEvent], task_id: TaskId) -> Blocker | None:
    """What the task's active attempt waits for a person about, if anything."""
    events = list(events)
    task = project(events).tasks.get(task_id)
    attempt = task.live_attempt if task else None
    if attempt is None or attempt.status is not AttemptStatus.ACTIVE:
        return None
    newest: StoredEvent | None = None
    for stored in events:
        event = stored.event
        if isinstance(event, AgentStateObserved) and event.attempt_id == attempt.attempt_id:
            newest = stored
    if newest is None or not isinstance(newest.event, AgentStateObserved):
        return None
    observed = newest.event
    if not observed.state.needs_human:
        return None
    return Blocker(
        task_id=task_id,
        attempt_id=attempt.attempt_id,
        agent=attempt.agent,
        seq=newest.seq,
        state=observed.state,
        evidence=observed.evidence or "",
    )


def allowed_actions(blocker: Blocker, adapter: AgentAdapter) -> tuple[OperatorAction, ...]:
    """The remote actions that fit ``blocker``; empty means "at the terminal"."""
    if blocker.state is AgentState.BLOCKED_PERMISSION:
        return tuple(
            action
            for action, approve in ((OperatorAction.APPROVE, True), (OperatorAction.DENY, False))
            if adapter.permission_keys(blocker.evidence, approve=approve)
        )
    # Free text only goes to an agent waiting at its own prompt.
    if blocker.state is AgentState.BLOCKED_QUESTION and blocker.evidence in ANSWERABLE_EVIDENCE:
        return (OperatorAction.ANSWER,)
    return ()


def _answered(events: Iterable[StoredEvent], blocker_seq: int) -> bool:
    return any(
        isinstance(s.event, OperatorActed) and s.event.blocker_seq == blocker_seq for s in events
    )


async def act(  # noqa: PLR0913 - the ports, the action and who takes it
    *,
    store: EventStore,
    host: PaneHost,
    adapters: Mapping[AgentKind, AgentAdapter],
    clock: Clock,
    task_id: TaskId,
    action: OperatorAction,
    by: str,
    blocker_seq: int | None = None,
    text: str = "",
) -> Blocker:
    """Carry out ``action`` on the task's current blocker, or raise ``ActionRefusedError``.

    ``blocker_seq`` is the blocker the operator was shown; ``None`` (the CLI) means
    "whatever the task waits for now".
    """
    events = list(await store.read())
    blocker = find_blocker(events, task_id)
    if blocker is None:
        raise ActionRefusedError(f"{task_id} is not waiting for a decision.")
    if blocker_seq is not None and blocker.seq != blocker_seq:
        raise ActionRefusedError(f"{task_id} has moved on; that question is no longer open.")
    if _answered(events, blocker.seq):
        raise ActionRefusedError(f"{task_id}: this was already answered.")
    adapter = adapters.get(blocker.agent)
    if adapter is None or action not in allowed_actions(blocker, adapter):
        raise ActionRefusedError(
            f"{task_id}: {action.value} does not fit this prompt ({blocker.state.value}); "
            "answer it at the terminal."
        )
    answer = one_line(text)
    if action is OperatorAction.ANSWER and not answer:
        raise ActionRefusedError("An answer needs some text.")

    attempt = project(events).tasks[task_id].live_attempt
    if attempt is None:  # find_blocker found it; kept for the type checker
        raise ActionRefusedError(f"{task_id} is not waiting for a decision.")
    try:
        target = await resolve_target(host, task_id, attempt, at=clock.now())
    except PaneHostError as err:
        raise ActionRefusedError(f"{task_id}: the agent's pane cannot be reached.") from err
    if target.events:
        await store.append(target.events)
    if not target.check.may_send or target.pane_id is None:
        raise ActionRefusedError(
            f"{task_id}: the pane no longer hosts this attempt ({target.check.value})."
        )

    if action is OperatorAction.ANSWER:
        await host.send_prompt(target.pane_id, answer)
    else:
        keys = adapter.permission_keys(blocker.evidence, approve=action is OperatorAction.APPROVE)
        await host.send_keys(target.pane_id, list(keys or ()))
    await store.append(
        [
            OperatorActed(
                occurred_at=clock.now(),
                task_id=task_id,
                attempt_id=blocker.attempt_id,
                action=action,
                blocker_seq=blocker.seq,
                by=by,
                chars=len(answer) if action is OperatorAction.ANSWER else 0,
            )
        ]
    )
    return blocker
