from __future__ import annotations

import io
import json
import sys
from typing import TYPE_CHECKING

from baton_herdr.core.logging import configure_logging, get_logger, log_context

if TYPE_CHECKING:
    import pytest


def test_log_lines_are_json_with_task_context(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = io.StringIO()
    monkeypatch.setattr(sys, "stderr", stream)
    configure_logging("info")

    with log_context(task_id="t-1", attempt_id="a-1"):
        get_logger("test").info("inside")
    get_logger("test").debug("filtered out")

    lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert len(lines) == 1
    assert lines[0] | {"timestamp": None} == {
        "event": "inside",
        "logger": "test",
        "level": "info",
        "task_id": "t-1",
        "attempt_id": "a-1",
        "timestamp": None,
    }


def test_logging_follows_a_replaced_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    first = io.StringIO()
    monkeypatch.setattr(sys, "stderr", first)
    configure_logging("info")
    logger = get_logger("test")
    first.close()  # e.g. a test runner's captured stream, closed after configuration

    second = io.StringIO()
    monkeypatch.setattr(sys, "stderr", second)
    logger.info("still logged")

    assert "still logged" in second.getvalue()
