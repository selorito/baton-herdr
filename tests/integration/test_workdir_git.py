"""What a task changed, read from a real git repository, without changing it."""

from __future__ import annotations

import shutil
import subprocess
from typing import TYPE_CHECKING

import pytest

from baton_herdr.core.changes import FileChange
from baton_herdr.workdir import GitWorkspace

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def git(repo: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603 - git with fixed arguments in a test repository
        ["git", "-C", str(repo), *args],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
            "HOME": str(repo),
        },
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (tmp_path / "old.py").write_text("x = 1\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-q", "-m", "start")
    return tmp_path


async def test_changes_since_the_start_cover_commits_edits_and_new_files(repo: Path) -> None:
    workspace = GitWorkspace()
    base = await workspace.head(str(repo))
    assert base is not None
    # The agent commits one change, leaves another uncommitted and creates a file.
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n\n\ndef power(a, b):\n")
    git(repo, "commit", "-q", "-am", "power")
    git(repo, "mv", "old.py", "new.py")
    (repo / "test_calc.py").write_text("import calc\nassert calc.add(1, 2) == 3\n")
    (repo / "logo.png").write_bytes(b"\x89PNG\0\0")
    status_before = git(repo, "status", "--porcelain")

    changes = await workspace.changes(str(repo), since=base)

    assert changes is not None
    assert sorted(changes.files, key=lambda f: f.path) == [
        FileChange("calc.py", 3, 0),
        FileChange("logo.png", None, None, new=True),
        FileChange("old.py → new.py", 0, 0),
        FileChange("test_calc.py", 2, 0, new=True),
    ]
    assert git(repo, "status", "--porcelain") == status_before  # nothing was touched


async def test_outside_a_repository_there_is_nothing_to_say(tmp_path: Path) -> None:
    workspace = GitWorkspace()
    assert await workspace.head(str(tmp_path)) is None
    assert await workspace.changes(str(tmp_path), since=None) is None
    assert await GitWorkspace(binary="no-such-git").changes(str(tmp_path), since=None) is None


async def test_without_a_start_commit_changes_since_the_last_commit_count(repo: Path) -> None:
    (repo / "calc.py").write_text("changed\n")
    changes = await GitWorkspace().changes(str(repo), since=None)
    assert changes is not None
    assert changes.files == (FileChange("calc.py", 1, 2),)
