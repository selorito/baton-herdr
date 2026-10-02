"""The real clock. Tests use :class:`baton_herdr.core.fakes.FixedClock`."""

from __future__ import annotations

from datetime import UTC, datetime


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)
