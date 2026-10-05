"""``git`` as the :class:`~baton_herdr.core.ports.Workspace`, read-only.

Only reading commands are run (``rev-parse``, ``diff --numstat``, ``ls-files``); the
repository is never changed. Anything that fails (no git, not a repository, a slow
disk) gives ``None``: a notice without a change summary, not a failed task.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

from baton_herdr.core.changes import Changes, FileChange

TIMEOUT_S = 10
# New files larger than this are not read to count their lines.
_COUNT_LIMIT = 1_000_000


class GitWorkspace:
    def __init__(self, binary: str = "git") -> None:
        self._binary = binary

    async def head(self, workdir: str) -> str | None:
        out = await self._git(workdir, "rev-parse", "--verify", "--quiet", "HEAD")
        if out is None:
            return None
        return out.strip() or None

    async def changes(self, workdir: str, since: str | None) -> Changes | None:
        if await self._git(workdir, "rev-parse", "--is-inside-work-tree") is None:
            return None
        files: list[FileChange] = []
        base = since or await self.head(workdir)
        if base is not None:
            numstat = await self._git(workdir, "diff", "--numstat", "-z", base, "--")
            if numstat is None:
                return None
            files.extend(_parse_numstat(numstat))
        untracked = await self._git(workdir, "ls-files", "--others", "--exclude-standard", "-z")
        if untracked is None:
            return None
        for path in filter(None, untracked.split("\0")):
            added = _count_lines(Path(workdir) / path)
            files.append(FileChange(path, added, 0 if added is not None else None, new=True))
        return Changes(tuple(files))

    async def _git(self, workdir: str, *args: str) -> str | None:
        try:
            process = await asyncio.create_subprocess_exec(
                self._binary,
                "-C",
                workdir,
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                stdin=asyncio.subprocess.DEVNULL,
            )
        except OSError:
            return None
        try:
            out, _ = await asyncio.wait_for(process.communicate(), TIMEOUT_S)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()
            return None
        if process.returncode != 0:
            return None
        return out.decode(errors="replace")


def _parse_numstat(text: str) -> list[FileChange]:
    """``git diff --numstat -z``: ``added\\tdeleted\\tpath\\0``; a rename has an empty path
    followed by ``old\\0new\\0``; a binary file has ``-`` for both counts."""
    fields = text.split("\0")
    files = []
    index = 0
    while index < len(fields) and fields[index]:
        added, deleted, path = fields[index].split("\t", 2)
        index += 1
        if not path:  # a rename: the old and the new path follow
            path = f"{fields[index]} → {fields[index + 1]}"
            index += 2
        files.append(
            FileChange(
                path,
                None if added == "-" else int(added),
                None if deleted == "-" else int(deleted),
            )
        )
    return files


def _count_lines(path: Path) -> int | None:
    """Lines in a new text file; ``None`` when it is binary, too large or unreadable."""
    try:
        if path.stat().st_size > _COUNT_LIMIT:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if b"\0" in data:
        return None
    return data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)
