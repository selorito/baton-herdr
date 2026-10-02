"""The real clock. Tests use :class:`coban.core.fakes.FixedClock`."""

from __future__ import annotations

from datetime import UTC, datetime


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)
