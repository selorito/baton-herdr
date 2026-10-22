"""What the assistant may know about tasks: a digest of the event log (ADR 0021). Pure.

Only what baton itself decided or observed goes in: ids, titles, statuses, agents, tiers,
interruption reasons and baton's words about them, the rule names that classified a
screen, and times. Never a task's instructions or request, a screen, a command read from
one, a directory, a setting or a token; and the whole is passed through ``redact`` once
more. Each fact carries what it can be cited by: the event's sequence number (``#12``) or
the attempt's short id (``a-1a2b3c4d``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from baton_herdr.core.events import (
    AgentChosen,
    AgentStateObserved,
    AttemptEnded,
    AttemptInterrupted,
    AttemptStarted,
    TaskCancelled,
    TaskCompleted,
    TaskEscalated,
    TaskFailed,
    TaskTierChanged,
)
from baton_herdr.core.model import AgentState
from baton_herdr.core.projection import project
from baton_herdr.core.redact import redact

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime, tzinfo

    from baton_herdr.core.events import StoredEvent
    from baton_herdr.core.model import AttemptId
    from baton_herdr.core.projection import TaskView

# Enough for a "why" about a project's tasks, short enough for a small model's context.
MAX_TASKS = 12
MAX_FACTS_PER_TASK = 12


@dataclass(frozen=True, slots=True)
class Digest:
    text: str
    # What a citation may name: "#12", "a-1a2b3c4d".
    cites: frozenset[str]
    # Ids asked about that are not in the log.
    unknown: tuple[str, ...] = ()
    # The tasks it covers, for the short form.
    tasks: tuple[TaskView, ...] = ()


def attempt_ref(attempt_id: AttemptId) -> str:
    return f"a-{str(attempt_id)[:8]}"


def build_digest(events: Sequence[StoredEvent], *, ids: Sequence[str], zone: tzinfo) -> Digest:
    """The digest of ``ids`` (tasks ``t-…``, or every task of a project ``p-…``); with no
    ids, of every open task."""
    board = project(events)
    unknown: list[str] = []
    chosen: list[TaskView] = []
    if ids:
        for wanted in ids:
            matched = [t for t in board.tasks.values() if wanted in {t.task_id, t.project}]
            if not matched:
                unknown.append(wanted)
            chosen += [t for t in matched if t not in chosen]
    else:
        chosen = [t for t in board.tasks.values() if not t.status.is_terminal]
    chosen = chosen[-MAX_TASKS:]
    lines: list[str] = []
    cites: set[str] = set()
    for task in chosen:
        facts = _facts(events, task, zone)[-MAX_FACTS_PER_TASK:]
        kind = f", {task.role} of {task.project}" if task.project else ""
        lines.append(f'{task.task_id} "{task.title}": {task.status.value}{kind}')
        for cite, text in facts:
            lines.append(f"  {cite} {text}")
            cites.add(cite)
        for attempt in task.attempts:
            cites.add(attempt_ref(attempt.attempt_id))
    text = redact("\n".join(lines)) if lines else "No tasks."
    return Digest(text=text, cites=frozenset(cites), unknown=tuple(unknown), tasks=tuple(chosen))


def brief(digest: Digest) -> list[str]:
    """One line per task, as /status writes them, for a question that needs no model."""
    lines = []
    for task in digest.tasks:
        live = task.attempts[-1] if task.attempts else None
        agent = live.agent.value if live else "-"
        known = live is not None and live.agent_state is not AgentState.UNKNOWN
        state = (
            f", {live.agent_state.value}" if live and known and not task.status.is_terminal else ""
        )
        reason = (
            f", stopped: {live.interrupt_reason.value}"
            if live and live.interrupt_reason and not task.status.is_terminal
            else ""
        )
        line = f"{task.task_id}  {task.status.value}  {agent}{state}{reason}  {task.title}"
        lines.append(redact(line))
    return lines


def _facts(  # noqa: PLR0912 - one branch per kind of event the digest tells
    events: Sequence[StoredEvent], task: TaskView, zone: tzinfo
) -> list[tuple[str, str]]:
    facts: list[tuple[str, str]] = []
    last_state: dict[AttemptId, str] = {}
    for stored in events:
        event = stored.event
        if event.task_id != task.task_id:
            continue
        at = _time(event.occurred_at, zone)
        cite = f"#{stored.seq}"
        match event:
            case AgentChosen():
                facts.append((cite, f"{at} chose {event.agent or 'no agent'}: {event.reason}"))
            case AttemptStarted():
                tier = f" ({event.tier.value})" if event.tier else ""
                facts.append(
                    (cite, f"{at} attempt {attempt_ref(event.attempt_id)} on {event.agent}{tier}")
                )
            case AgentStateObserved():
                state = f"{event.state.value} ({event.evidence or event.source})"
                if last_state.get(event.attempt_id) != state and event.state.needs_human:
                    facts.append((cite, f"{at} {attempt_ref(event.attempt_id)} {state}"))
                last_state[event.attempt_id] = state
            case AttemptInterrupted():
                until = (
                    f", not before {_time(event.resume_not_before, zone)}"
                    if (event.resume_not_before)
                    else ""
                )
                detail = f": {event.detail}" if event.detail else ""
                facts.append(
                    (
                        cite,
                        f"{at} {attempt_ref(event.attempt_id)} interrupted, "
                        f"{event.reason.value}{until}{detail}",
                    )
                )
            case AttemptEnded():
                facts.append(
                    (cite, f"{at} {attempt_ref(event.attempt_id)} ended, {event.outcome.value}")
                )
            case TaskEscalated():
                facts.append(
                    (
                        cite,
                        f"{at} moved up to premium on {event.agent} after {event.failures} "
                        f"economy failure(s), the last {event.reason.value}",
                    )
                )
            case TaskTierChanged():
                facts.append((cite, f"{at} tier set to {event.tier.value} by {event.by}"))
            case TaskCompleted():
                facts.append((cite, f"{at} completed"))
            case TaskFailed() | TaskCancelled():
                why = f": {event.reason}" if event.reason else ""
                facts.append((cite, f"{at} {event.type.removeprefix('task.')}{why}"))
            case _:
                pass  # prompts, permission decisions (a command read off a screen), notes
    return facts


def _time(at: datetime, zone: tzinfo) -> str:
    return f"{at.astimezone(zone):%Y-%m-%d %H:%M}"
