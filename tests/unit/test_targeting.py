from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from coban.core.events import AttemptLocated
from coban.core.fakes import FakePaneHost
from coban.core.model import AgentKind, AgentState, AttemptId, TaskId
from coban.core.panes import PaneObservation
from coban.core.projection import AttemptView
from coban.core.targeting import TargetCheck, check_target, resolve_target

NOW = datetime(2026, 9, 30, tzinfo=UTC)
T1 = TaskId("t1")
A1 = AttemptId(UUID(int=1))


def attempt(pane_id: str | None = "p1", session_ref: str | None = "s1") -> AttemptView:
    return AttemptView(
        attempt_id=A1, agent=AgentKind.CLAUDE, pane_id=pane_id, session_ref=session_ref
    )


def pane(
    pane_id: str = "p1",
    agent: AgentKind | None = AgentKind.CLAUDE,
    session_ref: str | None = "s1",
) -> PaneObservation:
    return PaneObservation(
        pane_id=pane_id,
        agent=agent,
        state=AgentState.IDLE,
        evidence="test",
        session_ref=session_ref,
    )


@pytest.mark.parametrize(
    ("the_attempt", "observation", "expected"),
    [
        (attempt(), pane(), TargetCheck.VERIFIED),
        (attempt(), pane(session_ref="s2"), TargetCheck.WRONG_SESSION),
        (attempt(), pane(session_ref=None), TargetCheck.SESSION_MISSING),
        (attempt(), pane(agent=AgentKind.CODEX), TargetCheck.WRONG_AGENT),
        (attempt(), pane(agent=None, session_ref=None), TargetCheck.NO_AGENT),
        (attempt(), None, TargetCheck.NO_PANE),
        (attempt(pane_id=None), pane(), TargetCheck.NO_PANE),
        # Before any session id is known, the agent kind is all there is.
        (attempt(session_ref=None), pane(session_ref=None), TargetCheck.UNCONFIRMED),
    ],
)
def test_check_target(
    the_attempt: AttemptView, observation: PaneObservation | None, expected: TargetCheck
) -> None:
    assert check_target(the_attempt, observation) is expected


def test_only_verified_and_unconfirmed_targets_may_receive_input() -> None:
    assert {c for c in TargetCheck if c.may_send} == {TargetCheck.VERIFIED, TargetCheck.UNCONFIRMED}


async def test_resolve_target_accepts_the_recorded_pane_when_the_session_matches() -> None:
    host = FakePaneHost()
    host.set_observation(pane())

    target = await resolve_target(host, T1, attempt(), at=NOW)

    assert (target.check, target.pane_id, target.events) == (TargetCheck.VERIFIED, "p1", ())


async def test_a_reused_pane_is_refused_even_though_the_pane_id_matches() -> None:
    host = FakePaneHost()
    host.set_observation(pane(session_ref="someone-elses-session"))

    target = await resolve_target(host, T1, attempt(), at=NOW)

    assert target.check is TargetCheck.WRONG_SESSION
    assert not target.check.may_send
    assert target.events == ()


@pytest.mark.parametrize("old_pane", ["gone", "shell", "other-session"])
async def test_a_session_found_in_another_pane_only_updates_the_locator(old_pane: str) -> None:
    host = FakePaneHost()
    if old_pane == "shell":
        host.set_observation(pane(agent=None, session_ref=None))
    elif old_pane == "other-session":
        host.set_observation(pane(session_ref="s9"))
    host.set_observation(pane(pane_id="p7"))

    target = await resolve_target(host, T1, attempt(), at=NOW)

    assert (target.check, target.pane_id) == (TargetCheck.VERIFIED, "p7")
    assert [type(e) for e in target.events] == [AttemptLocated]
    relocated = target.events[0]
    assert isinstance(relocated, AttemptLocated)
    assert (relocated.attempt_id, relocated.pane_id, relocated.session_ref) == (A1, "p7", None)
    assert host.sent == []  # resolving never sends anything


async def test_a_session_id_first_seen_on_the_pane_is_recorded() -> None:
    host = FakePaneHost()
    host.set_observation(pane(session_ref="fresh"))

    target = await resolve_target(host, T1, attempt(session_ref=None), at=NOW)

    assert (target.check, target.pane_id) == (TargetCheck.VERIFIED, "p1")
    learned = target.events[0]
    assert isinstance(learned, AttemptLocated)
    assert (learned.pane_id, learned.session_ref) == (None, "fresh")


async def test_a_lost_session_with_no_pane_anywhere_is_not_a_target() -> None:
    host = FakePaneHost()

    target = await resolve_target(host, T1, attempt(), at=NOW)

    assert (target.check, target.events) == (TargetCheck.NO_PANE, ())
