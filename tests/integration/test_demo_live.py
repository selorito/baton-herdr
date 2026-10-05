"""Roadmap slice 0 against a real herdr: ``just demo-test``.

Everything is real except the agents: a throwaway herdr session, the SQLite
event log, the herdr client, the adapters and the runner. The agents are
tools/fake-agent running under the names ``claude`` and ``codex``. Their launch
commands are set explicitly to the fake launchers, so no real agent can start
and no quota is spent.
"""

from __future__ import annotations

import shlex
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from baton_herdr.core.config import (
    BatonSettings,
    DatabaseSettings,
    DetectorSettings,
    SchedulerSettings,
)
from baton_herdr.core.events import AttemptInterrupted
from baton_herdr.core.fakes import RecordingNotifier
from baton_herdr.core.model import AgentKind, InterruptReason, TaskStatus
from baton_herdr.core.notify import NoticeKind
from baton_herdr.core.projection import project
from baton_herdr.daemon import add_task, open_runtime, run_pending

import fake_agent
from detector_binary import detector

if TYPE_CHECKING:
    from baton_herdr.herdr.host import HerdrPaneHost

pytestmark = pytest.mark.live

SCRIPTS = Path(__file__).parents[2] / "tools" / "fake-agent" / "scripts"


async def test_limited_claude_hands_off_to_codex_in_a_real_herdr(
    isolated_host: HerdrPaneHost, tmp_path: Path
) -> None:
    bin_dir = tmp_path / "bin"
    commands = {
        agent: shlex.join(
            ["env", "TZ=UTC", str(fake_agent.make_launcher(bin_dir, agent)), str(SCRIPTS / script)]
        )
        for agent, script in (("claude", "claude-limit.toml"), ("codex", "codex-finish.toml"))
    }
    settings = BatonSettings(
        database=DatabaseSettings(path=tmp_path / "baton.db"),
        detector=DetectorSettings(binary=str(detector())),
        scheduler=SchedulerSettings(
            agents=("claude", "codex"),
            launch_commands=commands,  # type: ignore[arg-type]
            timezone="UTC",
            start_timeout_seconds=30,
            turn_timeout_seconds=30,
            poll_interval_seconds=0.5,
        ),
    )
    notifier = RecordingNotifier()
    started_at = datetime.now(UTC)

    async with open_runtime(settings, host=isolated_host, notifier=notifier) as runtime:
        task_id = await add_task(
            runtime.store,
            title="power function",
            instructions="Add a power(a, b) function to calc.py with a test.",
            workdir=str(tmp_path),
        )
        results = await run_pending(runtime)
        events = await runtime.store.read()

    trail = "\n".join(
        f"{s.seq:3} {s.event.type:24} "
        + " ".join(
            f"{k}={v}"
            for k, v in s.event.model_dump(
                include={
                    "agent",
                    "state",
                    "evidence",
                    "reason",
                    "pane_id",
                    "session_ref",
                    "outcome",
                }
            ).items()
            if v is not None
        )
        for s in events
    )
    assert results == {task_id: TaskStatus.COMPLETED}, trail
    task = project(events).tasks[task_id]
    assert [a.agent for a in task.attempts] == [AgentKind.CLAUDE, AgentKind.CODEX]
    assert all(a.session_ref for a in task.attempts)  # learned from herdr, as with real hooks
    limit = next(s.event for s in events if isinstance(s.event, AttemptInterrupted))
    assert limit.reason is InterruptReason.RATE_LIMITED
    assert limit.resume_not_before is not None
    # The fake prints a reset 30 minutes ahead at minute precision.
    assert abs(limit.resume_not_before - (started_at + timedelta(minutes=30))) < timedelta(
        minutes=2
    )
    assert notifier.kinds == [
        NoticeKind.TASK_STARTED,
        NoticeKind.TASK_HANDED_OFF,
        NoticeKind.TASK_COMPLETED,
    ]
