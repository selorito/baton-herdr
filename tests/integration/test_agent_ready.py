"""The operator uses an agent again that baton set aside (ADR 0018)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID

import pytest
from typer.testing import CliRunner

from baton_herdr.budget.availability import fold_availability
from baton_herdr.cli import app
from baton_herdr.core.commands import complete_task
from baton_herdr.core.events import (
    AgentReleased,
    AttemptEnded,
    AttemptInterrupted,
    AttemptStarted,
    TaskCreated,
)
from baton_herdr.core.fakes import FixedClock, InMemoryEventStore
from baton_herdr.core.model import AgentKind, AttemptId, AttemptOutcome, InterruptReason, TaskId
from baton_herdr.core.projection import project
from baton_herdr.ledger import open_event_store
from baton_herdr.scheduler.operator import ActionRefusedError, release_agent

if TYPE_CHECKING:
    from pathlib import Path

NOW = datetime(2026, 10, 20, 0, 0, tzinfo=UTC)
TASK, ATTEMPT = TaskId("t-1"), AttemptId(UUID(int=1))
HOUR = timedelta(hours=1)


def signed_out_then_done(until: datetime = NOW + HOUR) -> list[object]:
    """Codex asked to be signed in; the task went on elsewhere and completed."""
    return [
        TaskCreated(occurred_at=NOW, task_id=TASK, title="t", instructions="x", workdir="/w"),
        AttemptStarted(occurred_at=NOW, task_id=TASK, attempt_id=ATTEMPT, agent=AgentKind.CODEX),
        AttemptInterrupted(
            occurred_at=NOW,
            task_id=TASK,
            attempt_id=ATTEMPT,
            reason=InterruptReason.SIGNED_OUT,
            resume_not_before=until,
        ),
        AttemptEnded(
            occurred_at=NOW, task_id=TASK, attempt_id=ATTEMPT, outcome=AttemptOutcome.ABANDONED
        ),
    ]


async def store_with(events: list[object]) -> InMemoryEventStore:
    store = InMemoryEventStore()
    await store.append(events)  # type: ignore[arg-type]
    task = project(await store.read()).tasks[TASK]
    await store.append(complete_task(task, at=NOW))
    return store


async def test_a_set_aside_agent_is_released_with_a_note_on_its_closed_task() -> None:
    store = await store_with(signed_out_then_done())
    clock = FixedClock(NOW + timedelta(minutes=5))

    until = await release_agent(
        store=store, clock=clock, agent=AgentKind.CODEX, by="cli", cooldown=HOUR
    )

    events = await store.read()
    assert until == NOW + HOUR
    assert isinstance(events[-1].event, AgentReleased)
    assert events[-1].event.attempt_id == ATTEMPT
    assert fold_availability(events, cooldown=HOUR).is_available(AgentKind.CODEX, clock.now())
    with pytest.raises(ActionRefusedError, match="codex is not set aside"):
        await release_agent(
            store=store, clock=clock, agent=AgentKind.CODEX, by="cli", cooldown=HOUR
        )
    with pytest.raises(ActionRefusedError, match="claude is not set aside"):
        await release_agent(
            store=store, clock=clock, agent=AgentKind.CLAUDE, by="cli", cooldown=HOUR
        )


def test_baton_agent_ready_from_the_command_line(tmp_path: Path) -> None:
    db = tmp_path / "cli.db"
    # Set aside far beyond the test's real clock.
    events = signed_out_then_done(until=datetime(2100, 1, 1, tzinfo=UTC))

    async def prepare() -> None:
        store = await open_event_store(db)
        try:
            await store.append(events)  # type: ignore[arg-type]
        finally:
            await store.close()

    asyncio.run(prepare())
    env = {"BATON_DATABASE__PATH": str(db), "BATON_LOGGING__LEVEL": "warning"}
    runner = CliRunner()

    released = runner.invoke(app, ["agent", "ready", "codex"], env=env)
    assert released.exit_code == 0, released.output
    assert released.stdout.startswith("codex is in use again (it was set aside until 2100-01-01")
    again = runner.invoke(app, ["agent", "ready", "codex"], env=env)
    assert again.exit_code == 1
    assert "codex is not set aside" in again.stderr
    assert runner.invoke(app, ["agent", "ready", "gpt"], env=env).exit_code == 2
