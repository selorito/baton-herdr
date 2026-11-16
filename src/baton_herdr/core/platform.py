"""Facts about the system baton runs on that change how it must work there.

The one place that knows which operating system, processor and Windows flavour baton is
on. Everything that differs between them asks here instead of testing ``sys.platform``
itself: the release binary to download, where herdr keeps its socket and how to reach
it, how to lock a file, how a service is installed.
"""

from __future__ import annotations

import contextlib
import os
import platform
import re
import shlex
import sys
import time
from pathlib import Path
from typing import IO, TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

OSRELEASE = Path("/proc/sys/kernel/osrelease")

type System = Literal["linux", "macos", "windows"]

# The processors baton's binaries are built for, by the names platform.machine() uses.
_MACHINES = {
    "x86_64": "x86_64",
    "amd64": "x86_64",
    "aarch64": "aarch64",
    "arm64": "aarch64",
}
# Release targets of baton-detect, by (system, processor).
RELEASE_TARGETS: dict[tuple[System, str], str] = {
    ("linux", "x86_64"): "x86_64-unknown-linux-gnu",
    ("linux", "aarch64"): "aarch64-unknown-linux-gnu",
    ("macos", "x86_64"): "x86_64-apple-darwin",
    ("macos", "aarch64"): "aarch64-apple-darwin",
    ("windows", "x86_64"): "x86_64-pc-windows-msvc",
}


def current_system(name: str | None = None) -> System:
    """``linux`` (WSL included), ``macos`` or ``windows`` (native)."""
    name = sys.platform if name is None else name
    if name == "win32":
        return "windows"
    if name == "darwin":
        return "macos"
    return "linux"


def machine(name: str | None = None) -> str:
    """The processor: ``x86_64`` or ``aarch64``, or what the system calls an other one."""
    raw = (platform.machine() if name is None else name).lower()
    return _MACHINES.get(raw, raw)


def release_target(system: System | None = None, processor: str | None = None) -> str | None:
    """baton-detect's release target for this system, or None when none is built."""
    return RELEASE_TARGETS.get((system or current_system(), processor or machine()))


def executable_name(name: str, system: System | None = None) -> str:
    """``name``, with ``.exe`` on Windows."""
    return f"{name}.exe" if (system or current_system()) == "windows" else name


def running_on_wsl1(osrelease: str | None = None) -> bool:
    """True on WSL1, whose kernel release reads like ``4.4.0-26100-Microsoft``.

    WSL2's (``…-microsoft-standard-WSL2``) is a real Linux kernel. WSL1 translates Linux
    calls into Windows ones and lacks some: a terminal's foreground process group (ADR
    0022), and shared memory that SQLite's WAL can rely on across processes.
    """
    release = _osrelease(osrelease)
    return release.endswith("-Microsoft") and "WSL2" not in release


def running_on_wsl(osrelease: str | None = None) -> bool:
    """True on WSL, either version."""
    return "microsoft" in _osrelease(osrelease).lower()


def _osrelease(osrelease: str | None) -> str:
    if osrelease is None:
        try:
            osrelease = OSRELEASE.read_text(encoding="utf-8")
        except OSError:
            return ""
    return osrelease.strip()


def describe(system: System | None = None, processor: str | None = None) -> str:
    """One line for ``doctor``: ``linux x86_64 (WSL1)``."""
    system = system or current_system()
    text = f"{system} {processor or machine()}"
    if system == "linux" and running_on_wsl():
        text += " (WSL1)" if running_on_wsl1() else " (WSL2)"
    return text


def herdr_config_dir(env: Mapping[str, str], home: Path, system: System | None = None) -> Path:
    """Where herdr keeps its config and sockets: ``$XDG_CONFIG_HOME/herdr`` or
    ``~/.config/herdr``; on Windows ``%APPDATA%\\herdr`` (herdr's own lookup)."""
    if (system or current_system()) == "windows":
        appdata = env.get("APPDATA")
        return (Path(appdata) if appdata else home / "AppData" / "Roaming") / "herdr"
    xdg_config_home = env.get("XDG_CONFIG_HOME")
    return (Path(xdg_config_home) if xdg_config_home else home / ".config") / "herdr"


def pipe_name(socket_path: Path) -> str:
    """The named pipe herdr serves its API on, on Windows, for its socket path.

    There ``herdr.sock`` is a marker file; the server listens on ``\\\\.\\pipe\\`` followed
    by the socket's path (herdr ``src/ipc.rs``, interprocess's ``GenericNamespaced``).
    """
    return "\\\\.\\pipe\\" + str(socket_path)


@contextlib.contextmanager
def exclusive_lock(file: IO[str], *, poll_s: float = 0.1) -> Iterator[None]:
    """Hold an exclusive lock on an open file until the block ends; waits for it."""
    if sys.platform == "win32":
        import msvcrt  # noqa: PLC0415 - Windows only

        file.seek(0)
        while True:
            try:
                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                time.sleep(poll_s)
        try:
            yield
        finally:
            file.seek(0)
            msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl  # noqa: PLC0415 - not on Windows

        fcntl.flock(file, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(file, fcntl.LOCK_UN)


def posix_permissions() -> bool:
    """POSIX permission bits mean something here. On Windows a file in the user's
    profile is already private to the user by its ACL; chmod only sets read-only."""
    return os.name == "posix"


# What a pane's shell takes literally without quotes: on POSIX shells and in PowerShell.
_PLAIN = re.compile(r"[A-Za-z0-9_./:=@%+,-]+")
_PLAIN_WINDOWS = re.compile(r"[A-Za-z0-9_./:\\=@%+,-]+")


def shell_quote(text: str, system: System | None = None) -> str:
    """``text`` as one word for the shell of a pane: a POSIX shell, or on Windows
    PowerShell, herdr's default there, where only ``'`` needs doubling inside ``'…'``.

    A double quote cannot be passed safely to a native program from Windows PowerShell
    5.1 (it strips them), so what carries JSON goes by file instead (``--settings``).
    """
    if (system or current_system()) == "windows":
        if _PLAIN_WINDOWS.fullmatch(text):
            return text
        return "'" + text.replace("'", "''") + "'"
    return text if _PLAIN.fullmatch(text) else shlex.quote(text)


def shell_join(words: Sequence[str], system: System | None = None) -> str:
    return " ".join(shell_quote(word, system) for word in words)
