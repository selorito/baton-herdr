"""What the operator is told, built from what the runner knows. Pure.

Each function returns a :class:`~baton_herdr.core.notify.Notice` whose parts are plain
text: the task's title, one sentence saying what happened, detail lines, and monospace
blocks (the end of the agent's screen, the files a task changed). Channels lay them out;
the snapshot tests pin both the wording and the Telegram layout.

Times are shown in the configured zone, as short as is still clear: ``17:00 +03``
today, ``Sun 09:30 +03`` within a week.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from baton_herdr.budget.report import tokens, when
from baton_herdr.core.model import InterruptReason
from baton_herdr.core.notify import Notice, NoticeKind
from baton_herdr.scheduler.text import screen_tail

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime, timedelta, tzinfo

    from baton_herdr.budget.quota import AgentBudget
    from baton_herdr.core.changes import Changes
    from baton_herdr.core.model import AgentKind, AttemptId, OperatorAction
    from baton_herdr.core.projection import TaskView
    from baton_herdr.core.usage import UsageTokens

# How a notice says why an attempt stopped.
STOPPED = {
    InterruptReason.RATE_LIMITED: "hit its usage limit",
    InterruptReason.CONTEXT_FULL: "ran out of context",
    InterruptReason.CRASHED: "crashed",
    InterruptReason.STALLED: "stopped responding",
    InterruptReason.OPERATOR: "was stopped by the operator",
    InterruptReason.RESUME_FAILED: "could not reopen its session",
}


def local_time(at: datetime, now: datetime, zone: tzinfo) -> str:
    return f"{when(at, now, zone)} {at.astimezone(zone):%Z}"


def _why(agent: AgentKind, reason: str) -> str:
    """The scheduler's reason without the agent's name it starts with."""
    return "Why: " + reason.removeprefix(f"{agent.value}: ")


def started(
    task: TaskView, agent: AgentKind, reason: str, attempt_id: AttemptId | None = None
) -> Notice:
    details = (_why(agent, reason),) if reason else ()
    return Notice(
        NoticeKind.TASK_STARTED,
        task.task_id,
        f"Started on {agent.value}.",
        attempt_id,
        title=task.title,
        details=details,
    )


def restarted(
    task: TaskView, agent: AgentKind, reason: str, attempt_id: AttemptId | None = None
) -> Notice:
    details = (_why(agent, reason),) if reason else ()
    return Notice(
        NoticeKind.TASK_RESTARTED,
        task.task_id,
        f"Restarted on {agent.value} in a fresh session.",
        attempt_id,
        title=task.title,
        details=details,
    )


def handed_off(  # noqa: PLR0913 - the move, why the first agent stopped, and why the next
    task: TaskView,
    previous: AgentKind,
    agent: AgentKind,
    *,
    stopped: InterruptReason | None,
    available_at: datetime | None,
    reason: str,
    now: datetime,
    zone: tzinfo,
    attempt_id: AttemptId | None = None,
) -> Notice:
    details = []
    if stopped is not None:
        line = f"{previous.value} {STOPPED[stopped]}"
        if available_at is not None:
            line += f"; available again at {local_time(available_at, now, zone)}"
        details.append(line + ".")
    if reason:
        details.append(_why(agent, reason))
    return Notice(
        NoticeKind.TASK_HANDED_OFF,
        task.task_id,
        f"Moved from {previous.value} to {agent.value}.",
        attempt_id,
        title=task.title,
        details=tuple(details),
    )


def resumed(task: TaskView, agent: AgentKind, attempt_id: AttemptId | None = None) -> Notice:
    return Notice(
        NoticeKind.TASK_RESUMED,
        task.task_id,
        f"Resuming the {agent.value} session.",
        attempt_id,
        title=task.title,
    )


def no_agent(task: TaskView, reason: str) -> Notice:
    """No agent can take the task now; ``reason`` is the scheduler's."""
    return Notice(
        NoticeKind.WAITING_FOR_AGENT,
        task.task_id,
        "No agent can take the task right now; it waits.",
        title=task.title,
        details=(reason,) if reason else (),
    )


def waiting_for_reset(  # noqa: PLR0913 - the attempt and when it may go on
    task: TaskView,
    agent: AgentKind,
    until: datetime | None,
    *,
    now: datetime,
    zone: tzinfo,
    attempt_id: AttemptId | None = None,
) -> Notice:
    when_text = f" at {local_time(until, now, zone)}" if until else " later"
    return Notice(
        NoticeKind.WAITING_FOR_AGENT,
        task.task_id,
        f"Every agent is limited; {agent.value} resumes this session{when_text}.",
        attempt_id,
        title=task.title,
    )


def needs_human(  # noqa: PLR0913 - what is asked, where, and what may be done remotely
    task: TaskView,
    text: str,
    *,
    screen: str = "",
    attempt_id: AttemptId | None = None,
    blocker_seq: int | None = None,
    actions: tuple[OperatorAction, ...] = (),
    policy: str = "",
) -> Notice:
    """``policy`` is what baton's policy said, when it left the decision to a person."""
    tail = screen_tail(screen)
    return Notice(
        NoticeKind.NEEDS_HUMAN,
        task.task_id,
        text,
        attempt_id,
        blocker_seq=blocker_seq,
        actions=actions,
        title=task.title,
        details=(policy,) if policy else (),
        blocks=(tail,) if tail else (),
    )


def stopped(  # noqa: PLR0913 - the attempt, why it stopped, and what was on screen
    task: TaskView,
    agent: AgentKind,
    reason: InterruptReason,
    *,
    detail: str = "",
    screen: str = "",
    attempt_id: AttemptId | None = None,
) -> Notice:
    """An attempt stopped for a reason other than a usage limit; recovery follows."""
    tail = screen_tail(screen)
    return Notice(
        NoticeKind.TASK_STOPPED,
        task.task_id,
        f"{agent.value} {STOPPED[reason]}.",
        attempt_id,
        title=task.title,
        details=(detail,) if detail else (),
        blocks=(tail,) if tail else (),
    )


def completed(  # noqa: PLR0913 - everything the operator wants to know about a finished task
    task: TaskView,
    agent: AgentKind,
    *,
    took: timedelta,
    attempts: int,
    spent: UsageTokens | None,
    budget: AgentBudget | None,
    changes: Changes | None,
    attempt_id: AttemptId | None = None,
) -> Notice:
    headline = f"Done on {agent.value} in {duration(took)}."
    if attempts > 1:
        headline = f"Done on {agent.value} in {duration(took)}, after {attempts} attempts."
    details = []
    if spent is not None:
        details.append(f"Tokens: {spent_text(spent)}")
    if budget is not None:
        details.append(f"{agent.value} budget left: {budget_left(budget)}")
    blocks: Sequence[str] = ()
    if changes is not None:
        blocks = (changes.stat(limit=5),)
    return Notice(
        NoticeKind.TASK_COMPLETED,
        task.task_id,
        headline,
        attempt_id,
        title=task.title,
        details=tuple(details),
        blocks=tuple(blocks),
    )


def duration(span: timedelta) -> str:
    seconds = max(0, int(span.total_seconds()))
    if seconds < 60:
        return f"{seconds} s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60} min"


def spent_text(spent: UsageTokens) -> str:
    """``12k in, 3k out, 1.2M cache read, 40k cache write``; zero parts left out."""
    parts = [
        (spent.input, "in"),
        (spent.output, "out"),
        (spent.cache_read, "cache read"),
        (spent.cache_write, "cache write"),
    ]
    shown = [f"{tokens(count)} {label}" for count, label in parts if count]
    return ", ".join(shown) or "none recorded"


def budget_left(budget: AgentBudget) -> str:
    if budget.remaining_percent is None:
        return "unknown"
    if budget.estimate:
        return f"~{budget.remaining_percent:.0f}% (estimate)"
    return f"{budget.remaining_percent:.0f}%"
