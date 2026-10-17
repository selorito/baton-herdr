"""``git`` as the :class:`~baton_herdr.core.ports.Repository`: a project's steps in
worktrees of their own, and their merges back (ADR 0017).

Unlike :class:`~baton_herdr.workdir.git.GitWorkspace` this writes, and only ever to the
repositories baton made for its projects. Commits are made as ``baton``, without hooks.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from pathlib import Path

from baton_herdr.core.ports import MergeResult, RepositoryError

TIMEOUT_S = 60
# baton commits as itself, unsigned, whatever the user's own git configuration says.
_IDENTITY = (
    "-c",
    "user.name=baton",
    "-c",
    "user.email=baton@localhost",
    "-c",
    "commit.gpgsign=false",
)


class GitRepository:
    def __init__(self, binary: str = "git") -> None:
        self._binary = binary

    async def commit_all(self, workdir: str, message: str) -> str:
        await self._git(workdir, "add", "--all")
        if (await self._git(workdir, "status", "--porcelain")).strip():
            await self._git(workdir, *_IDENTITY, "commit", "--quiet", "--no-verify", "-m", message)
        return await self._head(workdir)

    async def add_worktree(self, repo: str, path: str, branch: str) -> str:
        # A worktree left by a run that stopped before recording it is made again: nothing
        # has used it, since its step starts only once it is recorded.
        await self._run(repo, "worktree", "remove", "--force", path)
        await self._git(repo, "worktree", "prune")
        base = await self._head(repo)
        await self._git(repo, "worktree", "add", "--quiet", "-B", branch, path, base)
        return base

    async def merge(self, repo: str, branch: str, message: str) -> MergeResult:
        code, _, err = await self._run(
            repo, *_IDENTITY, "merge", "--no-ff", "--no-verify", "-m", message, branch
        )
        if code == 0:
            return MergeResult(commit=await self._head(repo))
        conflicts = await self._git(repo, "diff", "--name-only", "--diff-filter=U", "-z")
        files = tuple(sorted(filter(None, conflicts.split("\0"))))
        await self._run(repo, "merge", "--abort")
        if not files:
            msg = f"git merge {branch} in {repo} failed: {err.strip()[:300]}"
            raise RepositoryError(msg)
        return MergeResult(conflicts=files)

    async def finish_merge(self, repo: str, message: str) -> None:
        code, _, _ = await self._run(repo, "rev-parse", "-q", "--verify", "MERGE_HEAD")
        if code != 0:
            return
        unmerged = await self._git(repo, "diff", "--name-only", "--diff-filter=U", "-z")
        files = [f for f in unmerged.split("\0") if f]
        # A file still marked unmerged is resolved once no conflict marker is left in it,
        # whether or not whoever resolved it ran git add.
        marked = await asyncio.to_thread(_with_markers, Path(repo), files)
        if marked:
            msg = f"conflicts left unresolved in {repo}: {' '.join(marked)[:300]}"
            raise RepositoryError(msg)
        await self._git(repo, "add", "--all")
        await self._git(repo, *_IDENTITY, "commit", "--quiet", "--no-verify", "-m", message)

    async def contains(self, repo: str, branch: str) -> bool:
        code, _, _ = await self._run(repo, "merge-base", "--is-ancestor", branch, "HEAD")
        return code == 0

    async def remove_worktree(self, repo: str, path: str) -> None:
        await self._git(repo, "worktree", "remove", "--force", path)

    async def _head(self, workdir: str) -> str:
        return (await self._git(workdir, "rev-parse", "HEAD")).strip()

    async def _git(self, workdir: str, *args: str) -> str:
        code, out, err = await self._run(workdir, *args)
        if code != 0:
            msg = f"git {_verb(args)} in {workdir}: {err.strip()[:300]}"
            raise RepositoryError(msg)
        return out

    async def _run(self, workdir: str, *args: str) -> tuple[int, str, str]:
        """Exit code, standard output and standard error."""
        try:
            process = await asyncio.create_subprocess_exec(
                self._binary,
                "-C",
                workdir,
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL,
            )
        except OSError as err:
            msg = f"cannot run {self._binary}: {err}"
            raise RepositoryError(msg) from err
        try:
            out, errors = await asyncio.wait_for(process.communicate(), TIMEOUT_S)
        except TimeoutError as error:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()
            msg = f"git {_verb(args)} in {workdir} took longer than {TIMEOUT_S} s"
            raise RepositoryError(msg) from error
        code = process.returncode or 0
        return code, out.decode(errors="replace"), errors.decode(errors="replace")


_MARKER = re.compile(rb"^(<{7}|>{7})( |$)", re.MULTILINE)


def _with_markers(repo: Path, files: list[str]) -> list[str]:
    """The files that still hold a conflict marker (or cannot be read)."""
    marked = []
    for name in files:
        try:
            data = (repo / name).read_bytes()
        except FileNotFoundError:
            continue  # deleted: a resolution too
        except OSError:
            marked.append(name)
            continue
        if _MARKER.search(data):
            marked.append(name)
    return marked


def _verb(args: tuple[str, ...]) -> str:
    """The git command among ``args``, after the identity options."""
    rest = list(args)
    while rest[:1] == ["-c"]:
        rest = rest[2:]
    return rest[0] if rest else "?"
