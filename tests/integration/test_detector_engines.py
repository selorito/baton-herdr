"""The Rust classifier inside batond: the process, the shadow and fallback modes, and a
whole task run with both detectors answering every screen (ADR 0011, phase 2)."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
import structlog

from baton_herdr.adapters import ADAPTERS
from baton_herdr.core.config import DetectorSettings
from baton_herdr.core.detection import DetectionRequest, DetectionResult
from baton_herdr.core.detector import AdapterDetector
from baton_herdr.core.events import TaskCreated
from baton_herdr.core.fakes import FixedClock, InMemoryEventStore, RecordingNotifier
from baton_herdr.core.model import AgentKind, AgentState, TaskId, TaskStatus
from baton_herdr.daemon import make_detector
from baton_herdr.detector import (
    DetectorUnavailableError,
    FallbackDetector,
    ProcessDetector,
    ShadowDetector,
)
from baton_herdr.scheduler.runner import RunnerSettings, TaskRunner

from detector_binary import detector
from simulated_agents import SimulatedAgents

if TYPE_CHECKING:
    from pathlib import Path

NOW = datetime(2026, 10, 1, 11, 0, tzinfo=UTC)
LIMIT = DetectionRequest(
    agent=AgentKind.CLAUDE,
    screen="out\nYou've hit your session limit · resets 3:45pm\n❯\n",
    host_state=AgentState.IDLE,
    observed_at=NOW,
    timezone="Europe/Istanbul",
)
EXPECTED = DetectionResult(
    state=AgentState.RATE_LIMITED,
    evidence="baton:claude_usage_limit",
    resets_at=datetime(2026, 10, 1, 12, 45, tzinfo=UTC),
)


class Scripted:
    """A detector that answers from a list: a result, or an error to raise."""

    def __init__(self, *answers: DetectionResult | DetectorUnavailableError) -> None:
        self.answers = list(answers)
        self.asked = 0
        self.closed = False

    async def classify(self, request: DetectionRequest) -> DetectionResult:
        del request
        self.asked += 1
        answer = self.answers.pop(0)
        if isinstance(answer, DetectorUnavailableError):
            raise answer
        return answer

    async def aclose(self) -> None:
        self.closed = True


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def script(tmp_path: Path, body: str) -> str:
    path = tmp_path / "baton-detect"
    path.write_text(f"#!{sys.executable}\nimport sys, time\n{body}\n")
    path.chmod(0o755)
    return str(path)


async def test_the_process_answers_and_starts_again_after_a_bad_request() -> None:
    process = ProcessDetector(str(detector()))
    try:
        assert await process.classify(LIMIT) == EXPECTED
        bad = LIMIT.model_copy(update={"timezone": "Mars/Base"})
        with pytest.raises(DetectorUnavailableError, match="unknown time zone"):
            await process.classify(bad)
        assert await process.classify(LIMIT) == EXPECTED  # a fresh process
    finally:
        await process.aclose()


async def test_a_missing_slow_or_garbled_binary_is_unavailable(tmp_path: Path) -> None:
    with pytest.raises(DetectorUnavailableError, match="not found"):
        await ProcessDetector(str(tmp_path / "nowhere")).classify(LIMIT)
    slow = ProcessDetector(script(tmp_path, "time.sleep(30)"), timeout_s=0.2)
    with pytest.raises(DetectorUnavailableError, match="no answer within"):
        await slow.classify(LIMIT)
    await slow.aclose()
    garbled = ProcessDetector(script(tmp_path, "print('{}', flush=True); time.sleep(30)"))
    with pytest.raises(DetectorUnavailableError, match="unreadable answer"):
        await garbled.classify(LIMIT)
    await garbled.aclose()


async def test_shadow_keeps_the_primary_answer_and_counts_differences() -> None:
    other = EXPECTED.model_copy(update={"state": AgentState.IDLE})
    primary = Scripted(EXPECTED, EXPECTED, EXPECTED)
    shadow = Scripted(EXPECTED, other, DetectorUnavailableError("gone"))
    clock = Clock()
    detector_ = ShadowDetector(primary, shadow, retry_s=60, now=clock)

    with structlog.testing.capture_logs() as logs:
        for _ in range(3):
            assert await detector_.classify(LIMIT) == EXPECTED
        clock.t = 30
        primary.answers.append(EXPECTED)
        assert await detector_.classify(LIMIT) == EXPECTED  # shadow not asked while down

    assert (detector_.compared, detector_.mismatches, shadow.asked) == (2, 1, 3)
    events = [entry["event"] for entry in logs]
    assert events == ["detector mismatch", "detector unavailable"]
    mismatch = logs[0]
    assert (mismatch["python"][0], mismatch["rust"][0]) == ("rate_limited", "idle")
    assert "screen" not in mismatch
    await detector_.aclose()
    assert primary.closed
    assert shadow.closed


async def test_fallback_answers_while_the_primary_is_down_and_retries_later() -> None:
    fallback_answer = EXPECTED.model_copy(update={"evidence": "python"})
    primary = Scripted(DetectorUnavailableError("crashed"), EXPECTED)
    fallback = Scripted(fallback_answer, fallback_answer)
    clock = Clock()
    detector_ = FallbackDetector(primary, fallback, retry_s=60, now=clock)

    assert await detector_.classify(LIMIT) == fallback_answer
    clock.t = 59
    assert await detector_.classify(LIMIT) == fallback_answer
    assert primary.asked == 1  # not retried within retry_s
    clock.t = 60
    assert await detector_.classify(LIMIT) == EXPECTED


@pytest.mark.parametrize(
    ("engine", "kind"),
    [("python", AdapterDetector), ("shadow", ShadowDetector), ("rust", FallbackDetector)],
)
def test_the_engine_setting_picks_the_detector(engine: str, kind: type) -> None:
    assert isinstance(make_detector(DetectorSettings(engine=engine)), kind)  # type: ignore[arg-type]


async def test_a_whole_task_run_gets_the_same_answers_from_both_detectors() -> None:
    """Slice 0 (a limit, a hand-off, a finished turn) with Rust shadowing Python."""
    store = InMemoryEventStore()
    task = TaskId("power")
    await store.append(
        [
            TaskCreated(
                occurred_at=NOW, task_id=task, title="p", instructions="Add power().", workdir="/w"
            )
        ]
    )
    shadow = ShadowDetector(AdapterDetector(ADAPTERS), ProcessDetector(str(detector())))
    runner = TaskRunner(
        store=store,
        host=SimulatedAgents(FixedClock(NOW)),
        adapters=ADAPTERS,
        notifier=RecordingNotifier(),
        clock=FixedClock(NOW),
        settings=RunnerSettings(start_timeout_s=2, turn_timeout_s=2, poll_interval_s=0.05),
        detector=shadow,
    )
    try:
        assert await runner.run(task) is TaskStatus.COMPLETED
    finally:
        await shadow.aclose()
    assert shadow.compared > 0
    assert shadow.mismatches == 0
