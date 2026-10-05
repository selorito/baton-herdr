"""Snapshots of every notice as Telegram receives it.

Each case builds a notice the way the runner does (``baton_herdr.scheduler.notices``) and
renders it with ``format_notice``; the HTML must equal ``snapshots/notices/<case>.html``.
A changed message is a deliberate change: regenerate with
``UPDATE_SNAPSHOTS=1 uv run pytest tests/unit/test_notices.py`` and review the diff.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import pytest

from baton_herdr.budget.quota import AgentBudget, BudgetSource
from baton_herdr.core.changes import Changes, FileChange
from baton_herdr.core.model import AgentKind, InterruptReason, OperatorAction, TaskId
from baton_herdr.core.projection import TaskView
from baton_herdr.core.usage import UsageTokens
from baton_herdr.scheduler import notices
from baton_herdr.telegram.notifier import as_shown, format_notice

if TYPE_CHECKING:
    from baton_herdr.core.notify import Notice

SNAPSHOTS = Path(__file__).parent / "snapshots" / "notices"
NOW = datetime(2026, 10, 5, 11, 0, tzinfo=UTC)  # a Monday, 14:00 in Istanbul
ZONE = ZoneInfo("Europe/Istanbul")
TASK = TaskView(task_id=TaskId("t-90f0f859"), title="power function")
CLAUDE, CODEX = AgentKind.CLAUDE, AgentKind.CODEX
SCREEN = "\n".join(
    [
        "earlier output that is cut off",
        *(f"line {n}" for n in range(1, 10)),
        "  ⎿  Error: OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuvwx is invalid <401>",
        "",
    ]
)
CHANGES = Changes(
    (
        FileChange("calc.py", 10, 2),
        FileChange("test_calc.py", 8, 0),
        FileChange("docs/power.md", 14, 0, new=True),
        FileChange("logo.png", None, None),
        FileChange("old.py → new.py", 0, 0),
        FileChange("README.md", 1, 1),
        FileChange("CHANGELOG.md", 3, 0),
    )
)

CASES: dict[str, Notice] = {
    "started": notices.started(TASK, CLAUDE, "claude: first in preference order, 62% left."),
    "started-low-budget": notices.started(
        TASK,
        CODEX,
        "codex: next in preference order (claude ~4% left (estimate), below the 10% reserve), "
        "budget unknown.",
    ),
    "restarted": notices.restarted(TASK, CLAUDE, "claude: first in preference order."),
    "handed-off-limit": notices.handed_off(
        TASK,
        CLAUDE,
        CODEX,
        stopped=InterruptReason.RATE_LIMITED,
        available_at=NOW + timedelta(hours=2, minutes=30),
        reason="codex: next in preference order (claude limited until 2026-10-05 16:30), 71% left.",
        now=NOW,
        zone=ZONE,
    ),
    "handed-off-tomorrow": notices.handed_off(
        TASK,
        CLAUDE,
        CODEX,
        stopped=InterruptReason.RATE_LIMITED,
        available_at=NOW + timedelta(days=1),
        reason="codex: next in preference order.",
        now=NOW,
        zone=ZONE,
    ),
    "resumed": notices.resumed(TASK, CLAUDE),
    "no-agent": notices.no_agent(
        TASK, "No agent can take the task: claude limited until 2026-10-05 16:30."
    ),
    "waiting-for-reset": notices.waiting_for_reset(
        TASK, CLAUDE, NOW + timedelta(hours=1), now=NOW, zone=ZONE
    ),
    "permission": notices.needs_human(
        TASK,
        "claude asks for permission: Bash command · rm -rf build/ · Remove the build directory",
        screen=SCREEN,
        blocker_seq=42,
        actions=(OperatorAction.APPROVE, OperatorAction.DENY),
    ),
    "question": notices.needs_human(
        TASK,
        "claude is waiting for a decision: Should divide(a, 0) raise or return inf?",
        screen=SCREEN,
        blocker_seq=43,
        actions=(OperatorAction.ANSWER,),
    ),
    "crashed": notices.stopped(TASK, CODEX, InterruptReason.CRASHED, screen=SCREEN),
    "stalled": notices.stopped(
        TASK, CLAUDE, InterruptReason.STALLED, detail="No progress during work."
    ),
    "done": notices.completed(
        TASK,
        CLAUDE,
        took=timedelta(minutes=12, seconds=30),
        attempts=1,
        spent=UsageTokens(input=12_345, output=3_210, cache_read=1_200_000, cache_write=40_000),
        budget=AgentBudget(CLAUDE, BudgetSource.LEARNED, 61.4),
        changes=CHANGES,
    ),
    "done-after-hand-off": notices.completed(
        TASK,
        CODEX,
        took=timedelta(hours=1, minutes=5),
        attempts=2,
        spent=UsageTokens(input=900, output=120, cache_read=0, cache_write=0),
        budget=AgentBudget(CODEX, BudgetSource.REPORTED, 88),
        changes=Changes(()),
    ),
    "done-without-data": notices.completed(
        TASK, CODEX, took=timedelta(seconds=40), attempts=1, spent=None, budget=None, changes=None
    ),
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_notice_snapshot(name: str) -> None:
    rendered = format_notice(CASES[name])
    path = SNAPSHOTS / f"{name}.html"
    if os.environ.get("UPDATE_SNAPSHOTS"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered + "\n", encoding="utf-8")
    assert rendered + "\n" == path.read_text(encoding="utf-8")


def test_every_snapshot_has_a_case() -> None:
    assert {p.stem for p in SNAPSHOTS.glob("*.html")} == set(CASES)


@pytest.mark.parametrize("name", sorted(CASES))
def test_no_notice_carries_a_secret(name: str) -> None:
    assert "sk-proj-" not in as_shown(format_notice(CASES[name]))


def test_a_decision_ends_with_the_reference_a_reply_needs() -> None:
    shown = as_shown(format_notice(CASES["question"]))
    assert shown.splitlines()[-1] == "t-90f0f859 #43"
    assert shown.splitlines()[0] == "power function"
