"""A new project's directory: made by baton, never named by the request (ADR 0016)."""

from __future__ import annotations

import asyncio
import re
import unicodedata
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

TIMEOUT_S = 30
# Letters NFKD does not take apart: Turkish dotless i and dotted capital I, German sharp s.
_SPELLED = str.maketrans({"\u0131": "i", "\u0130": "I", "\u00df": "ss"})


class ProjectSetupError(Exception):
    """The directory or its repository could not be made."""


def new_project_id() -> str:
    return f"p-{uuid.uuid4().hex[:6]}"


def slug(request: str, *, limit: int = 32) -> str:
    """A directory name from the request's first words: ``a-website-for-my-cafe``."""
    spelled = request.translate(_SPELLED)
    ascii_text = unicodedata.normalize("NFKD", spelled).encode("ascii", "ignore").decode()
    words = re.findall(r"[a-z0-9]+", ascii_text.lower())
    name = ""
    for word in words:
        candidate = f"{name}-{word}" if name else word
        if len(candidate) > limit:
            break
        name = candidate
    return name or "project"


async def create_project_dir(root: Path, project_id: str, request: str) -> Path:
    """``<root>/<slug>-<id>``, a git repository with one empty commit, so a project's
    changes are measured from its start. ``.baton/`` (the plan) is kept out of them."""
    directory = root / f"{slug(request)}-{project_id.removeprefix('p-')}"
    try:
        directory.mkdir(parents=True)
    except OSError as err:
        msg = f"cannot create {directory}: {err.strerror}"
        raise ProjectSetupError(msg) from err
    (directory / ".baton").mkdir()
    await _git(directory, "init", "--quiet", "--initial-branch", "main")
    exclude = directory / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    with exclude.open("a", encoding="utf-8") as file:
        file.write(".baton/\n")
    await _git(
        directory,
        "-c",
        "user.name=baton",
        "-c",
        "user.email=baton@localhost",
        "commit",
        "--quiet",
        "--allow-empty",
        "--message",
        f"start {project_id}",
    )
    return directory


async def _git(directory: Path, *args: str) -> None:
    try:
        process = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=directory,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, err = await asyncio.wait_for(process.communicate(), TIMEOUT_S)
    except (OSError, TimeoutError) as error:
        msg = f"git {args[0]} in {directory}: {error}"
        raise ProjectSetupError(msg) from error
    if process.returncode != 0:
        msg = f"git {' '.join(args[:2])} in {directory}: {err.decode(errors='replace').strip()}"
        raise ProjectSetupError(msg)
