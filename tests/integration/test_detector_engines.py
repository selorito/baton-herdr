"""The Rust classifier inside batond: the process, the fallback while it is down, and a
whole task run on each (ADR 0011)."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from baton_herdr.adapters import ADAPTERS
from baton_herdr.core.config import DetectorSettings
from baton_herdr.core.detection import DetectionRequest, DetectionResult
from baton_herdr.core.detector import UNAVAILABLE_EVIDENCE, HostDetector
from baton_herdr.core.events import TaskCreated
from baton_herdr.core.fakes import FixedClock, InMemoryEventStore, RecordingNotifier
from baton_herdr.core.model import AgentKind, AgentState, TaskId, TaskStatus
from baton_herdr.core.notify import NoticeKind
from baton_herdr.daemon import make_detector
from baton_herdr.detector import DetectorUnavailableError, FallbackDetector, ProcessDetector
from baton_herdr.scheduler.runner import RunnerSettings, TaskRunner

from detector_binary import detector
from simulated_agents import SimulatedAgents

if TYPE_CHECKING:
    from pathlib import Path

    from baton_herdr.core.detector import Detector

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


@pytest.mark.parametrize("engine", ["rust", "python", "shadow"])
def test_every_engine_setting_is_baton_detect_with_herdr_behind_it(engine: str) -> None:
    assert isinstance(make_detector(DetectorSettings(engine=engine)), FallbackDetector)  # type: ignore[arg-type]


async def test_without_the_classifier_idle_means_nothing_and_the_rest_passes() -> None:
    host = HostDetector()
    idle = await host.classify(LIMIT)  # a limit on screen, herdr says idle
    assert (idle.state, idle.evidence) == (AgentState.UNKNOWN, UNAVAILABLE_EVIDENCE)
    working = LIMIT.model_copy(update={"host_state": AgentState.WORKING, "host_evidence": "h"})
    assert (await host.classify(working)).state is AgentState.WORKING
    blocked = LIMIT.model_copy(update={"host_state": AgentState.BLOCKED_OTHER})
    assert (await host.classify(blocked)).state is AgentState.BLOCKED_OTHER
    gone = LIMIT.model_copy(update={"agent_running": False, "host_state": AgentState.UNKNOWN})
    assert (await host.classify(gone)).evidence == "host:no-agent"


async def test_a_task_run_on_the_classifier_hands_off_on_a_limit() -> None:
    """Slice 0 (a limit, a hand-off, a finished turn) on baton-detect."""
    status, _ = await _run_task(ProcessDetector(str(detector())))
    assert status is TaskStatus.COMPLETED


async def test_while_the_classifier_is_down_a_limit_is_never_a_finished_task(
    tmp_path: Path,
) -> None:
    down = FallbackDetector(ProcessDetector(str(tmp_path / "nowhere")), HostDetector())
    status, notifier = await _run_task(down)
    assert status is not TaskStatus.COMPLETED
    assert NoticeKind.TASK_COMPLETED not in notifier.kinds


async def _run_task(detector_: Detector) -> tuple[TaskStatus, RecordingNotifier]:
    store = InMemoryEventStore()
    task = TaskId("power")
    await store.append(
        [
            TaskCreated(
                occurred_at=NOW, task_id=task, title="p", instructions="Add power().", workdir="/w"
            )
        ]
    )
    notifier = RecordingNotifier()
    runner = TaskRunner(
        store=store,
        host=SimulatedAgents(FixedClock(NOW)),
        adapters=ADAPTERS,
        notifier=notifier,
        clock=FixedClock(NOW),
        settings=RunnerSettings(start_timeout_s=1, turn_timeout_s=1, poll_interval_s=0.05),
        detector=detector_,
    )
    try:
        return await runner.run(task), notifier
    finally:
        await detector_.aclose()
