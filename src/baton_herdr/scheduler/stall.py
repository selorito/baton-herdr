"""Whether a working agent has stopped making progress (ADR 0014). Pure.

Progress is either of two signals:

- the screen changes, ignoring what moves on its own while a request is pending (clocks,
  elapsed-time counters, spinner frames);
- the collector records tokens for the attempt's session: each response the agent gets.

A model that thinks for a long time shows neither for a while, so one signal alone is not
enough: the screen may stay put during a long response, and the tokens of a response are
only recorded once it ends. Only when both have been quiet longer than the agents' own
request timeouts is the agent taken to be stuck.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime, timedelta

# What changes on a screen while nothing happens: elapsed-time counters ("3m 12s", "2s"),
# clocks, running token counts ("12.4k tokens"), spinner frames (braille and stars) and
# animated ellipses. Other numbers stay: "Step 2 of 9" after "Step 1 of 9" is progress.
_MOVING = re.compile(
    r"\b\d+(?:\.\d+)?\s?(?:ms|s|m|h|sec|secs|min|mins)\b"
    r"|\b\d{1,2}:\d{2}(?::\d{2})?\b"
    r"|\b\d+(?:[.,]\d+)?\s?[kKmM]?\s?tokens?\b"
    r"|[\u2800-\u28ff✻✶✳✢✽·•*…]|\.{2,}"
)


_SPACES = re.compile(r"[ \t]+")


def fingerprint(screen: str) -> str:
    """The screen without what moves by itself, hashed."""
    still = _SPACES.sub(" ", _MOVING.sub("", screen))
    return hashlib.blake2b(still.encode(), digest_size=16).hexdigest()


@dataclass(slots=True)
class ProgressWatch:
    """When a working agent last showed progress, by either signal."""

    since: datetime | None = None
    _screen: str | None = None

    def working(self, screen: str, now: datetime) -> None:
        """The agent is seen working with ``screen``; a changed screen is progress."""
        key = fingerprint(screen)
        if self.since is None or key != self._screen:
            self._screen, self.since = key, now

    def tokens(self, at: datetime) -> None:
        """Tokens were recorded at ``at``: progress, if it is newer than what was seen."""
        if self.since is not None and at > self.since:
            self.since = at

    def pause(self) -> None:
        """Not working (waiting for a person, idle): the clock starts again at the next work."""
        self.since = self._screen = None

    def quiet_for(self, now: datetime) -> timedelta | None:
        return None if self.since is None else now - self.since

    def quiet(self, now: datetime, limit: timedelta) -> bool:
        quiet = self.quiet_for(now)
        return quiet is not None and quiet >= limit
