from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from coban.budget.availability import Availability, fold_availability
from coban.core.events import AttemptInterrupted, AttemptStarted, Event, StoredEvent
from coban.core.model import AgentKind, AttemptId, InterruptReason, TaskId

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
T1 = TaskId("t1")
CLAUDE_ATTEMPT = AttemptId(UUID(int=1))
CODEX_ATTEMPT = AttemptId(UUID(int=2))


def log(*events: Event) -> list[StoredEvent]:
    return [StoredEvent(seq=i, event=e) for i, e in enumerate(events, start=1)]


def started(attempt: AttemptId, agent: AgentKind) -> AttemptStarted:
    return AttemptStarted(occurred_at=NOW, task_id=T1, attempt_id=attempt, agent=agent)


def interrupted(
    attempt: AttemptId, reason: InterruptReason, until: datetime | None = None
) -> AttemptInterrupted:
    return AttemptInterrupted(
        occurred_at=NOW, task_id=T1, attempt_id=attempt, reason=reason, resume_not_before=until
    )


def test_an_agent_is_unavailable_until_its_printed_reset_time() -> None:
    reset = NOW + timedelta(minutes=45)
    view = fold_availability(
        log(
            started(CLAUDE_ATTEMPT, AgentKind.CLAUDE),
            interrupted(CLAUDE_ATTEMPT, InterruptReason.RATE_LIMITED, reset),
        )
    )

    agents = [AgentKind.CLAUDE, AgentKind.CODEX]
    assert view.available(agents, NOW) == [AgentKind.CODEX]
    assert view.available(agents, reset) == [AgentKind.CLAUDE, AgentKind.CODEX]


def test_without_a_reset_time_the_cooldown_applies() -> None:
    events = log(
        started(CLAUDE_ATTEMPT, AgentKind.CLAUDE),
        interrupted(CLAUDE_ATTEMPT, InterruptReason.RATE_LIMITED),
    )
    view = fold_availability(events, cooldown=timedelta(minutes=10))

    assert not view.is_available(AgentKind.CLAUDE, NOW + timedelta(minutes=9))
    assert view.is_available(AgentKind.CLAUDE, NOW + timedelta(minutes=10))


def test_other_interruptions_do_not_touch_availability() -> None:
    view = fold_availability(
        log(
            started(CODEX_ATTEMPT, AgentKind.CODEX),
            interrupted(CODEX_ATTEMPT, InterruptReason.CRASHED),
        )
    )
    assert view == Availability(attempt_agents={CODEX_ATTEMPT: AgentKind.CODEX})


def test_a_later_report_extends_but_never_shortens_the_wait() -> None:
    late, early = NOW + timedelta(hours=3), NOW + timedelta(hours=1)
    view = fold_availability(
        log(
            started(CLAUDE_ATTEMPT, AgentKind.CLAUDE),
            interrupted(CLAUDE_ATTEMPT, InterruptReason.RATE_LIMITED, late),
        )
    )
    view = fold_availability(
        [
            StoredEvent(
                seq=3, event=interrupted(CLAUDE_ATTEMPT, InterruptReason.RATE_LIMITED, early)
            )
        ],
        start=view,
    )
    assert view.limited_until[AgentKind.CLAUDE] == late
