"""Decide whether a pane really hosts an attempt before anything is sent to it.

An attempt is identified by its own id. Its pane and agent session are
locations, and panes get reused: a pane id that once held the attempt may now
hold a shell, another agent, or another session of the same agent. Typing a
prompt into the wrong one is the failure this module exists to prevent
(ADR 0006).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from coban.core.events import AttemptLocated, Event
from coban.core.panes import PaneNotFoundError

if TYPE_CHECKING:
    from datetime import datetime

    from coban.core.model import TaskId
    from coban.core.panes import PaneHost, PaneObservation
    from coban.core.projection import AttemptView


class TargetCheck(StrEnum):
    VERIFIED = "verified"  # the pane hosts the attempt's agent session
    UNCONFIRMED = "unconfirmed"  # right agent; no session id is known yet on either side
    NO_PANE = "no_pane"  # the attempt has no pane, or the pane is gone
    NO_AGENT = "no_agent"  # the pane runs no agent
    WRONG_AGENT = "wrong_agent"  # the pane runs a different kind of agent
    WRONG_SESSION = "wrong_session"  # same agent kind, another session
    SESSION_MISSING = "session_missing"  # the attempt has a session id, the pane reports none

    @property
    def may_send(self) -> bool:
        """Input may go to the pane only for these outcomes."""
        return self in {TargetCheck.VERIFIED, TargetCheck.UNCONFIRMED}


def check_target(attempt: AttemptView, observation: PaneObservation | None) -> TargetCheck:
    """Compare an attempt with a fresh observation of its recorded pane.

    Once an attempt has a session id, only a pane reporting that same id is
    accepted. Before any session id is known (just after launch, or for an agent
    that never reports one) the agent kind is all there is to compare.
    """
    if attempt.pane_id is None or observation is None:
        return TargetCheck.NO_PANE
    if observation.agent is not attempt.agent:
        return TargetCheck.NO_AGENT if observation.agent is None else TargetCheck.WRONG_AGENT
    if attempt.session_ref is None:
        return TargetCheck.UNCONFIRMED
    if observation.session_ref == attempt.session_ref:
        return TargetCheck.VERIFIED
    if observation.session_ref is None:
        return TargetCheck.SESSION_MISSING
    return TargetCheck.WRONG_SESSION


@dataclass(frozen=True, slots=True)
class Target:
    check: TargetCheck
    # The pane to use when ``check.may_send``; otherwise the last known pane, if any.
    pane_id: str | None
    # Locator updates discovered on the way; append them before sending anything.
    events: tuple[Event, ...] = field(default=())


async def resolve_target(
    host: PaneHost, task_id: TaskId, attempt: AttemptView, *, at: datetime
) -> Target:
    """Find where ``attempt`` lives now. Never sends anything.

    - The recorded pane is observed and checked.
    - If it does not check out and the attempt has a session id, the session is
      looked up across panes; a hit only updates the locator.
    - A session id first seen on the verified pane is recorded.
    """
    observation = await _observe(host, attempt.pane_id)
    check = check_target(attempt, observation)

    if check is TargetCheck.UNCONFIRMED and observation is not None and observation.session_ref:
        learned = AttemptLocated(
            occurred_at=at,
            task_id=task_id,
            attempt_id=attempt.attempt_id,
            session_ref=observation.session_ref,
        )
        return Target(TargetCheck.VERIFIED, attempt.pane_id, (learned,))

    if not check.may_send and attempt.session_ref is not None:
        moved_to = await host.find_session(attempt.session_ref)
        if moved_to is not None and moved_to != attempt.pane_id:
            relocated = AttemptLocated(
                occurred_at=at, task_id=task_id, attempt_id=attempt.attempt_id, pane_id=moved_to
            )
            return Target(TargetCheck.VERIFIED, moved_to, (relocated,))

    return Target(check, attempt.pane_id)


async def _observe(host: PaneHost, pane_id: str | None) -> PaneObservation | None:
    if pane_id is None:
        return None
    try:
        return await host.observe(pane_id)
    except PaneNotFoundError:
        return None
