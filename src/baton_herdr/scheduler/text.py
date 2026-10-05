"""Screen text cut down for a notice."""

from __future__ import annotations

TAIL_LINES = 8
TAIL_WIDTH = 160


def screen_tail(screen: str, lines: int = TAIL_LINES, width: int = TAIL_WIDTH) -> str:
    """The last ``lines`` non-blank lines of ``screen``, each cut to ``width`` characters."""
    kept = [line.rstrip() for line in screen.splitlines() if line.strip()][-lines:]
    return "\n".join(line if len(line) <= width else line[: width - 1] + "…" for line in kept)
