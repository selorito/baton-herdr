from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from baton_herdr.budget.availability import Availability, fold_availability
from baton_herdr.core.events import (
    AgentReleased,
    AttemptInterrupted,
    AttemptStarted,
    Event,
    StoredEvent,
)
from baton_herdr.core.model import AgentKind, AttemptId, InterruptReason, TaskId

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


def test_an_agent_that_asked_to_be_signed_in_is_unavailable_like_a_limited_one() -> None:
    later = NOW + timedelta(hours=1)
    view = fold_availability(
        log(
            started(CODEX_ATTEMPT, AgentKind.CODEX),
            interrupted(CODEX_ATTEMPT, InterruptReason.SIGNED_OUT, until=later),
        )
    )
    assert not view.is_available(AgentKind.CODEX, NOW)
    assert view.is_available(AgentKind.CODEX, later + timedelta(seconds=1))


def test_a_released_agent_is_available_until_it_is_set_aside_again() -> None:
    later = NOW + timedelta(hours=1)
    aside: list[Event] = [
        started(CODEX_ATTEMPT, AgentKind.CODEX),
        interrupted(CODEX_ATTEMPT, InterruptReason.SIGNED_OUT, until=later),
    ]
    released = AgentReleased(occurred_at=NOW, task_id=T1, attempt_id=CODEX_ATTEMPT, by="cli")
    assert fold_availability(log(*aside, released)).is_available(AgentKind.CODEX, NOW)
    again = interrupted(CODEX_ATTEMPT, InterruptReason.RATE_LIMITED, until=later)
    assert not fold_availability(log(*aside, released, again)).is_available(AgentKind.CODEX, NOW)
