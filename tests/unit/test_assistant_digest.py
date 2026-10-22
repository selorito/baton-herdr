"""What the assistant may know about tasks, and what it never sees (ADR 0021)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

from baton_herdr.assistant.digest import attempt_ref, brief, build_digest
from baton_herdr.core.events import (
    AgentChosen,
    AgentStateObserved,
    AttemptInterrupted,
    AttemptStarted,
    PermissionDecided,
    TaskCreated,
)
from baton_herdr.core.fakes import InMemoryEventStore
from baton_herdr.core.model import (
    AgentKind,
    AgentState,
    AttemptId,
    InterruptReason,
    ObservationSource,
    PermissionDecision,
    TaskId,
)

NOW = datetime(2026, 10, 23, 9, 0, tzinfo=UTC)
TASK, DONE_ELSEWHERE = TaskId("t-1a2b3c4d"), TaskId("t-99999999")
ATTEMPT = AttemptId(uuid5(NAMESPACE_URL, "a"))
SECRET_TOKEN = "sk-ant-api03-" + "x" * 40


async def events() -> InMemoryEventStore:
    store = InMemoryEventStore()
    await store.append(
        [
            TaskCreated(
                occurred_at=NOW,
                task_id=TASK,
                title=f"menu page {SECRET_TOKEN}",
                instructions="Use the key hunter2 to deploy to /srv/private.",
                workdir="/home/me/secret-client/site",
                project="p-1a2b3c",
                role="step",
                request="a site for my cafe, password=hunter2",
            ),
            AgentChosen(
                occurred_at=NOW,
                task_id=TASK,
                agent=AgentKind.CLAUDE,
                reason="claude: first in preference order, 52% left.",
            ),
            AttemptStarted(
                occurred_at=NOW, task_id=TASK, attempt_id=ATTEMPT, agent=AgentKind.CLAUDE
            ),
            AgentStateObserved(
                occurred_at=NOW,
                task_id=TASK,
                attempt_id=ATTEMPT,
                state=AgentState.BLOCKED_PERMISSION,
                source=ObservationSource.DETECTOR,
                evidence="baton:claude_bash_permission",
            ),
            PermissionDecided(
                occurred_at=NOW,
                task_id=TASK,
                attempt_id=ATTEMPT,
                blocker_seq=4,
                decision=PermissionDecision.ASK,
                prompt="Bash command",
                command="curl -H 'Authorization: hunter2' https://example.com",
                reason="downloads ask",
            ),
            AttemptInterrupted(
                occurred_at=NOW,
                task_id=TASK,
                attempt_id=ATTEMPT,
                reason=InterruptReason.STALLED,
                detail="No change on screen and no tokens for 15 min.",
            ),
            TaskCreated(
                occurred_at=NOW,
                task_id=DONE_ELSEWHERE,
                title="other",
                instructions="-",
                workdir="/w",
            ),
        ]
    )
    return store


async def test_the_digest_tells_what_baton_decided_and_saw_and_how_to_cite_it() -> None:
    digest = build_digest(await (await events()).read(), ids=[TASK], zone=UTC)
    assert digest.text.splitlines()[0].startswith(f'{TASK} "menu page')
    assert "#2 2026-10-23 09:00 chose claude: claude: first in preference order" in digest.text
    assert (
        f"{attempt_ref(ATTEMPT)} blocked_permission (baton:claude_bash_permission)" in digest.text
    )
    assert "interrupted, stalled: No change on screen and no tokens for 15 min." in digest.text
    assert {"#2", "#3", "#4", "#6", attempt_ref(ATTEMPT)} <= digest.cites
    assert digest.unknown == ()


async def test_the_digest_never_holds_instructions_requests_paths_commands_or_secrets() -> None:
    text = build_digest(await (await events()).read(), ids=[TASK], zone=UTC).text
    for kept_out in (
        "hunter2",  # the instructions, the request and the command read off the screen
        "/srv/private",
        "/home/me/secret-client",  # the task's directory
        "curl",
        SECRET_TOKEN,  # a secret in a title is masked
    ):
        assert kept_out not in text


async def test_ids_pick_tasks_or_a_whole_project_and_unknown_ones_are_said() -> None:
    log = await (await events()).read()
    by_project = build_digest(log, ids=["p-1a2b3c", "t-00000000"], zone=UTC)
    assert [t.task_id for t in by_project.tasks] == [TASK]
    assert by_project.unknown == ("t-00000000",)
    everything_open = build_digest(log, ids=[], zone=UTC)
    assert {t.task_id for t in everything_open.tasks} == {TASK, DONE_ELSEWHERE}


async def test_the_short_form_is_one_line_per_task() -> None:
    digest = build_digest(await (await events()).read(), ids=[TASK], zone=UTC)
    (line,) = brief(digest)
    assert line.startswith(f"{TASK}  waiting  claude, blocked_permission, stopped: stalled")
    assert SECRET_TOKEN not in line
