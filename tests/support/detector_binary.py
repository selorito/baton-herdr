"""Where the tests find baton-detect: BATON_DETECT_BIN, else target/debug, else PATH.

Without a binary the test is skipped, unless BATON_REQUIRE_DETECT is set (CI sets it),
in which case it fails.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from baton_herdr.core.detection import DetectionRequest, DetectionResult
from baton_herdr.detector import ProcessDetector

REPO = Path(__file__).parents[2]


def detector() -> Path:
    candidates = [
        os.environ.get("BATON_DETECT_BIN"),
        str(REPO / "target" / "debug" / "baton-detect"),
        shutil.which("baton-detect"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    if os.environ.get("BATON_REQUIRE_DETECT"):
        pytest.fail("baton-detect is required but was not found")
    pytest.skip("baton-detect is not built")


# Detectors handed to runners in this test; closed by the autouse fixture in conftest.py.
_OPEN: list[ProcessDetector] = []


def rust_detector() -> ProcessDetector:
    """A running ``baton-detect classify`` for a ``TaskRunner``; closed after the test."""
    process = ProcessDetector(str(detector()))
    _OPEN.append(process)
    return process


async def close_detectors() -> None:
    while _OPEN:
        await _OPEN.pop().aclose()


def classify(*requests: DetectionRequest) -> list[DetectionResult]:
    """Classify with one ``baton-detect classify`` run, for tests without an event loop."""
    result = subprocess.run(  # noqa: S603 - the binary under test, fixed arguments
        [str(detector()), "classify"],
        input="".join(r.model_dump_json() + "\n" for r in requests),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return [DetectionResult.model_validate_json(line) for line in result.stdout.splitlines()]


def classify_one(request: DetectionRequest) -> DetectionResult:
    (result,) = classify(request)
    return result
