"""Facts about the system baton runs on that change how it must work there."""

from __future__ import annotations

from pathlib import Path

OSRELEASE = Path("/proc/sys/kernel/osrelease")


def running_on_wsl1(osrelease: str | None = None) -> bool:
    """True on WSL1, whose kernel release reads like ``4.4.0-26100-Microsoft``.

    WSL2's (``…-microsoft-standard-WSL2``) is a real Linux kernel. WSL1 translates Linux
    calls into Windows ones and lacks some: a terminal's foreground process group (ADR
    0022), and shared memory that SQLite's WAL can rely on across processes.
    """
    if osrelease is None:
        try:
            osrelease = OSRELEASE.read_text(encoding="utf-8")
        except OSError:
            return False
    release = osrelease.strip()
    return release.endswith("-Microsoft") and "WSL2" not in release
