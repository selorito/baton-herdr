"""Structured JSON logging.

Every log line is a single JSON object. ``task_id`` and ``attempt_id`` are
optional context fields: bind them with :func:`log_context` and every line
emitted inside the block carries them, including across ``await`` points.
"""

from __future__ import annotations

import logging
import sys
from contextlib import contextmanager
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from collections.abc import Iterator

    from coban.core.config import LogLevel


class _CurrentStderr:
    """Writes to whatever ``sys.stderr`` is at the moment of writing.

    Binding the stream object at configuration time breaks when it is later
    replaced and closed (test runners, daemonising); logging must never raise.
    """

    def write(self, text: str) -> int:
        return sys.stderr.write(text)

    def flush(self) -> None:
        sys.stderr.flush()


def configure_logging(level: LogLevel = "info") -> None:
    """Configure structlog to write JSON lines to stderr."""
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.dict_tracebacks,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[level.upper()]
        ),
        logger_factory=structlog.PrintLoggerFactory(file=_CurrentStderr()),  # type: ignore[arg-type]
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.typing.FilteringBoundLogger:
    logger: structlog.typing.FilteringBoundLogger = structlog.get_logger().bind(logger=name)
    return logger


@contextmanager
def log_context(*, task_id: str | None = None, attempt_id: str | None = None) -> Iterator[None]:
    """Attach ``task_id`` / ``attempt_id`` to every log line in this block."""
    fields = {
        key: value
        for key, value in (("task_id", task_id), ("attempt_id", attempt_id))
        if value is not None
    }
    with structlog.contextvars.bound_contextvars(**fields):
        yield
