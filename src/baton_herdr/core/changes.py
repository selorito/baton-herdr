"""What a task changed in its working directory, for the notice that it is done."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FileChange:
    path: str
    # Lines added and removed; None for a binary file.
    added: int | None
    deleted: int | None
    # Not tracked by git yet: a file the task created.
    new: bool = False


@dataclass(frozen=True, slots=True)
class Changes:
    files: tuple[FileChange, ...]

    def stat(self, limit: int = 5) -> str:
        """Like ``git diff --stat``, numbers instead of bars, at most ``limit`` files:

        ``calc.py  +10 -2`` per file, then ``… 3 more``, then the totals.
        """
        if not self.files:
            return "No files changed."
        shown = self.files[:limit]
        width = max(len(change.path) for change in shown)
        lines = [f"{change.path:<{width}}  {_counts(change)}" for change in shown]
        if len(self.files) > limit:
            lines.append(f"… {len(self.files) - limit} more")
        added = sum(change.added or 0 for change in self.files)
        deleted = sum(change.deleted or 0 for change in self.files)
        count = len(self.files)
        lines.append(f"{count} file{'s' if count != 1 else ''}, +{added} -{deleted}")
        return "\n".join(lines)


def _counts(change: FileChange) -> str:
    if change.added is None or change.deleted is None:
        counts = "binary"
    else:
        counts = (
            " ".join(
                part
                for part in (
                    f"+{change.added}" if change.added else "",
                    f"-{change.deleted}" if change.deleted else "",
                )
                if part
            )
            or "±0"
        )
    return f"new, {counts}" if change.new else counts
