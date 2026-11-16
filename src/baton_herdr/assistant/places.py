"""Where the assistant may offer work in a directory that exists (ADR 0021, amended).

The model names the directory as the operator wrote it, or a project's id; baton decides.
Only a directory inside the policy's ``trusted_dirs``, or one of the projects baton made,
is offered, and only once it exists: baton never creates, moves or deletes a directory
for it. Links are followed first, so a link inside a trusted directory is judged by where
it leads. Anything else is refused with the reason, and no card is shown.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import TYPE_CHECKING, Literal

from baton_herdr.core.projection import project

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from baton_herdr.core.assistant import Directories
    from baton_herdr.core.events import StoredEvent

_PROJECT_ID = re.compile(r"p-[0-9a-f]{6}")

type RefusalReason = Literal["relative", "missing", "untrusted", "unknown_project"]


@dataclass(frozen=True, slots=True)
class Place:
    """A directory work may go to, and why: ``"trusted_dirs"``, or the project's id."""

    path: str
    by: str


@dataclass(frozen=True, slots=True)
class Refused:
    """A directory named as it was written, and why no work goes there."""

    path: str
    reason: RefusalReason


@dataclass(frozen=True, slots=True)
class Places:
    directories: Directories
    # The policy's trusted_dirs, resolved; none when the policy is off.
    trusted: tuple[str, ...] = ()

    def find(self, path: str, events: Iterable[StoredEvent]) -> Place | Refused:
        """Where ``path`` (a directory as written, or a project id) leads, if work may go
        there; checked against the log's projects as they are in ``events``."""
        named = path.strip()
        projects = known_projects(events)
        candidate = named
        if _PROJECT_ID.fullmatch(named):
            if named not in projects:
                return Refused(path, "unknown_project")
            candidate = projects[named]
        elif not (named.startswith(("/", "~")) or PurePath(named).is_absolute()):
            # Absolute: /home/…, ~/…, or on Windows C:\Users\… (and /… on the current drive).
            return Refused(path, "relative")
        resolved = self.directories.resolve(candidate)
        if resolved is None:
            return Refused(path, "missing")
        where = PurePath(resolved)
        if any(where.is_relative_to(trusted) for trusted in self.trusted):
            return Place(resolved, "trusted_dirs")
        for project_id, workdir in projects.items():
            if where.is_relative_to(workdir):
                return Place(resolved, project_id)
        return Refused(path, "untrusted")


def known_projects(events: Iterable[StoredEvent]) -> Mapping[str, str]:
    """Each project in the log and the directory baton made for it (its plan's)."""
    tasks = project(events).tasks.values()
    return {t.project: t.workdir for t in tasks if t.role == "plan" and t.project is not None}


class LocalDirectories:
    """:class:`baton_herdr.core.assistant.Directories` on this machine. It only looks."""

    def resolve(self, path: str) -> str | None:
        try:
            where = Path(path).expanduser().resolve(strict=True)
        except (OSError, RuntimeError):  # missing, unreadable, or ~user unknown
            return None
        return str(where) if where.is_absolute() and where.is_dir() else None
