"""What the runner puts in its notices: the agent and why, the screen when it needs a
person, and, when done, the time, the tokens, the changes and the budget left."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from baton_herdr.adapters import ADAPTERS
from baton_herdr.budget.load import BudgetReader
from baton_herdr.core.changes import Changes, FileChange
from baton_herdr.core.events import AttemptStarted, TaskCreated
from baton_herdr.core.fakes import (
    FixedClock,
    InMemoryEventStore,
    InMemoryUsageStore,
    RecordingNotifier,
)
from baton_herdr.core.model import AgentKind, TaskId, TaskStatus
from baton_herdr.core.notify import NoticeKind
from baton_herdr.core.usage import UsageRecord, UsageTokens
from baton_herdr.scheduler.runner import RunnerSettings, TaskRunner

from detector_binary import rust_detector
from simulated_agents import SimulatedAgents

NOW = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)
TASK = TaskId("t-90f0f859")
SETTINGS = RunnerSettings(
    agents=(AgentKind.CLAUDE,),
    start_timeout_s=2,
    turn_timeout_s=2,
    poll_interval_s=0.05,
    usage_grace_s=0,
)


class FakeWorkspace:
    def __init__(self) -> None:
        self.asked_since: list[str | None] = []

    async def head(self, workdir: str) -> str | None:
        assert workdir == "/work/calc"
        return "abc123"

    async def changes(self, workdir: str, since: str | None) -> Changes | None:
        del workdir
        self.asked_since.append(since)
        return Changes((FileChange("calc.py", 10, 2), FileChange("test_calc.py", 8, 0, new=True)))


def record(
    record_id: str, session: str, at: datetime, total: int, agent: str = "claude"
) -> UsageRecord:
    return UsageRecord(
        agent=agent,  # type: ignore[arg-type]
        session_id=session,
        record_id=record_id,
        at=at,
        tokens=UsageTokens(input=total, output=total // 10, cache_read=0, cache_write=0),
    )


async def setup(
    clock: FixedClock, **simulation: object
) -> tuple[InMemoryEventStore, RecordingNotifier, TaskRunner, FakeWorkspace, InMemoryUsageStore]:
    store = InMemoryEventStore()
    await store.append(
        [
            TaskCreated(
                occurred_at=NOW,
                task_id=TASK,
                title="power function",
                instructions="Add power(a, b).",
                workdir="/work/calc",
            )
        ]
    )
    usage = InMemoryUsageStore()
    workspace = FakeWorkspace()
    notifier = RecordingNotifier()
    runner = TaskRunner(
        detector=rust_detector(),
        store=store,
        host=SimulatedAgents(
            clock,
            scripts={AgentKind.CLAUDE: "claude-finish.toml"},
            **simulation,  # type: ignore[arg-type]
        ),
        adapters=ADAPTERS,
        notifier=notifier,
        clock=clock,
        settings=SETTINGS,
        budget=BudgetReader(usage=usage, cooldown=timedelta(hours=1), claude_window_tokens=100_000),
        workspace=workspace,
    )
    return store, notifier, runner, workspace, usage


async def test_the_done_notice_sums_the_attempts_own_tokens_and_lists_the_changes() -> None:
    clock = FixedClock(NOW)
    store, notifier, runner, workspace, usage = await setup(clock)
    await usage.record(
        [
            record("before", "claude-session", NOW - timedelta(hours=1), 50_000),  # earlier
            record("mine-1", "claude-session", NOW, 1_000),
            record("mine-2", "claude-session", NOW, 2_000),
            record("other", "someone-elses", NOW, 9_000),  # another session at the same time
            record("codex", "c", NOW, 7_000, agent="codex"),
        ]
    )

    assert await runner.run(TASK) is TaskStatus.COMPLETED

    started, done = notifier.notices
    assert (started.title, started.text, started.details) == (
        "power function",
        "Started on claude.",
        ("Why: first in preference order, ~32% left (estimate).",),
    )
    assert done.kind is NoticeKind.TASK_COMPLETED
    assert done.text == "Done on claude in 0 s."
    assert done.details == (
        "Tokens: 3k in, 300 out",  # only the attempt's own session, from its start
        "claude budget left: ~32% (estimate)",
    )
    assert done.blocks == ("calc.py       +10 -2\ntest_calc.py  new, +8\n2 files, +18 -2",)
    # The changes are measured from the commit the task started on.
    events = [s.event for s in await store.read()]
    (start,) = [e for e in events if isinstance(e, AttemptStarted)]
    assert start.workdir_head == "abc123"
    assert workspace.asked_since == ["abc123"]


async def test_a_question_brings_the_end_of_the_screen() -> None:
    _, notifier, runner, _, _ = await setup(FixedClock(NOW), permission_after_prompt=True)

    assert await runner.run(TASK) is TaskStatus.NEEDS_HUMAN

    asked = notifier.notices[-1]
    assert asked.kind is NoticeKind.NEEDS_HUMAN
    assert asked.title == "power function"
    (tail,) = asked.blocks
    assert "Do you want to proceed?" in tail
    assert len(tail.splitlines()) <= 8
