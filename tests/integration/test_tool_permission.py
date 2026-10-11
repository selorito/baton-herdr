"""Claude's tool permissions other than Bash's come with Approve and Deny. Their first
choice may change the user's settings for good, so Approve picks the one-time "Yes" it
reads from the screen as it is when the button is pressed, and is refused without one."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from baton_herdr.adapters import ADAPTERS
from baton_herdr.adapters.claude import TOOL_PERMISSION_EVIDENCE, one_time_yes
from baton_herdr.core.events import AgentStateObserved, AttemptLocated, AttemptStarted, TaskCreated
from baton_herdr.core.fakes import FakePaneHost, FixedClock, InMemoryEventStore
from baton_herdr.core.model import (
    AgentKind,
    AgentState,
    AttemptId,
    ObservationSource,
    OperatorAction,
    TaskId,
)
from baton_herdr.core.panes import PaneObservation
from baton_herdr.scheduler.operator import ActionRefusedError, act, allowed_actions, find_blocker

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
TASK = TaskId("t-read")
ATTEMPT = AttemptId(UUID(int=1))
RECORDED = (
    Path(__file__).parents[2]
    / "fixtures/claude/blocked_permission/20261011T114435Z/screen.detection.txt"
).read_text()
# The same dialog where every "Yes" keeps allowing: nothing to approve just once.
ONLY_FOR_GOOD = RECORDED.replace("4. Yes, but ask again next time", "4. Yes, always")
CLAUDE = ADAPTERS[AgentKind.CLAUDE]


def test_the_one_time_yes_is_read_from_the_choices() -> None:
    assert one_time_yes(RECORDED) == "4"  # not 1, which keeps allowing for good
    assert one_time_yes(ONLY_FOR_GOOD) is None
    assert one_time_yes(" Do you want to proceed?\n ❯ 1. Yes\n   2. No\n") == "1"
    assert one_time_yes("") is None
    assert CLAUDE.permission_summary(RECORDED) == (
        "Read outside the working directories · Read(/etc/hostname)"
    )
    assert CLAUDE.permission_keys(TOOL_PERMISSION_EVIDENCE, approve=True) is None  # no screen
    assert CLAUDE.permission_keys(TOOL_PERMISSION_EVIDENCE, approve=False) == ("Escape",)


async def blocked(screen: str) -> tuple[InMemoryEventStore, FakePaneHost]:
    store = InMemoryEventStore()
    await store.append(
        [
            TaskCreated(occurred_at=NOW, task_id=TASK, title="t", instructions="x", workdir="/w"),
            AttemptStarted(
                occurred_at=NOW, task_id=TASK, attempt_id=ATTEMPT, agent=AgentKind.CLAUDE
            ),
            AttemptLocated(
                occurred_at=NOW, task_id=TASK, attempt_id=ATTEMPT, pane_id="p1", session_ref="s1"
            ),
            AgentStateObserved(
                occurred_at=NOW,
                task_id=TASK,
                attempt_id=ATTEMPT,
                state=AgentState.BLOCKED_PERMISSION,
                source=ObservationSource.DETECTOR,
                evidence=TOOL_PERMISSION_EVIDENCE,
            ),
        ]
    )
    host = FakePaneHost()
    host.set_observation(
        PaneObservation(
            pane_id="p1",
            agent=AgentKind.CLAUDE,
            state=AgentState.BLOCKED_PERMISSION,
            evidence=TOOL_PERMISSION_EVIDENCE,
            session_ref="s1",
        )
    )
    host.screens["p1"] = screen
    return store, host


async def press(store: InMemoryEventStore, host: FakePaneHost, action: OperatorAction) -> None:
    await act(
        store=store,
        host=host,
        adapters=ADAPTERS,
        clock=FixedClock(NOW),
        task_id=TASK,
        action=action,
        by="telegram:7",
    )


async def test_approve_picks_the_one_time_yes_on_the_screen_as_it_is_now() -> None:
    store, host = await blocked(RECORDED)
    blocker = find_blocker(await store.read(), TASK)
    assert blocker is not None
    assert allowed_actions(blocker, CLAUDE, RECORDED) == (
        OperatorAction.APPROVE,
        OperatorAction.DENY,
    )

    await press(store, host, OperatorAction.APPROVE)
    assert ("p1", "keys", ("4",)) in host.sent


async def test_without_a_one_time_yes_only_deny_is_offered() -> None:
    store, host = await blocked(ONLY_FOR_GOOD)
    blocker = find_blocker(await store.read(), TASK)
    assert blocker is not None
    assert allowed_actions(blocker, CLAUDE, ONLY_FOR_GOOD) == (OperatorAction.DENY,)

    with pytest.raises(ActionRefusedError, match="answer it at the terminal"):
        await press(store, host, OperatorAction.APPROVE)
    assert not [sent for sent in host.sent if sent[1] == "keys"]
    await press(store, host, OperatorAction.DENY)
    assert ("p1", "keys", ("Escape",)) in host.sent
