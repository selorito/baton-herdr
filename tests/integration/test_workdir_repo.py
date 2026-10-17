"""A project's steps in worktrees, and their merges, on real git repositories (ADR 0017)."""

from __future__ import annotations

import shutil
import subprocess
from typing import TYPE_CHECKING

import pytest

from baton_herdr.core.ports import RepositoryError
from baton_herdr.workdir import GitRepository

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def git(repo: Path, *args: str, check: bool = True) -> str:
    return subprocess.run(  # noqa: S603 - git with fixed arguments in a test repository
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@x", *args],  # noqa: S607
        capture_output=True,
        text=True,
        check=check,
    ).stdout


def names(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir() if p.name != ".git")


@pytest.fixture
def project(tmp_path: Path) -> Path:
    repo = tmp_path / "site"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "commit", "-q", "--allow-empty", "-m", "start")
    return repo


async def test_steps_in_worktrees_are_merged_back_one_after_another(
    project: Path, tmp_path: Path
) -> None:
    repo = GitRepository()
    (project / "index.html").write_text("<h1>Cafe</h1>\n")
    plan = await repo.commit_all(str(project), "baton: plan")
    menu, style = tmp_path / "wt" / "step-1", tmp_path / "wt" / "step-2"
    assert await repo.add_worktree(str(project), str(menu), "baton/step-1") == plan
    assert await repo.add_worktree(str(project), str(style), "baton/step-2") == plan
    (menu / "menu.html").write_text("coffee\n")
    (style / "style.css").write_text("h1 { color: brown }\n")
    await repo.commit_all(str(menu), "step 1")
    await repo.commit_all(str(style), "step 2")

    first = await repo.merge(str(project), "baton/step-1", "merge step 1")
    second = await repo.merge(str(project), "baton/step-2", "merge step 2")

    assert first.merged
    assert second.merged
    assert second.commit == git(project, "rev-parse", "HEAD").strip()
    assert names(project) == [
        "index.html",
        "menu.html",
        "style.css",
    ]
    assert await repo.contains(str(project), "baton/step-1")
    await repo.remove_worktree(str(project), str(menu))
    assert not menu.exists()
    assert await repo.contains(str(project), "baton/step-1")  # the branch stays


async def test_nothing_to_commit_keeps_the_head(project: Path) -> None:
    head = git(project, "rev-parse", "HEAD").strip()
    assert await GitRepository().commit_all(str(project), "nothing") == head


async def test_a_conflicting_merge_is_aborted_and_names_its_files(
    project: Path, tmp_path: Path
) -> None:
    repo = GitRepository()
    (project / "index.html").write_text("<h1>Cafe</h1>\n")
    await repo.commit_all(str(project), "plan")
    one, two = tmp_path / "one", tmp_path / "two"
    await repo.add_worktree(str(project), str(one), "baton/step-1")
    await repo.add_worktree(str(project), str(two), "baton/step-2")
    (one / "index.html").write_text("<h1>Cafe Menu</h1>\n")
    (two / "index.html").write_text("<h1>Cafe Hours</h1>\n")
    await repo.commit_all(str(one), "step 1")
    await repo.commit_all(str(two), "step 2")
    assert (await repo.merge(str(project), "baton/step-1", "merge 1")).merged

    result = await repo.merge(str(project), "baton/step-2", "merge 2")

    assert not result.merged
    assert result.conflicts == ("index.html",)
    assert git(project, "status", "--porcelain") == ""  # left as it was
    assert not await repo.contains(str(project), "baton/step-2")

    # Someone resolves it in the project directory; baton commits what they left.
    git(project, "merge", "baton/step-2", check=False)
    (project / "index.html").write_text("<h1>Cafe Menu and Hours</h1>\n")
    await repo.finish_merge(str(project), "merge 2, resolved")
    assert await repo.contains(str(project), "baton/step-2")
    await repo.finish_merge(str(project), "nothing under way")  # no merge: nothing happens


async def test_a_failure_says_which_command(project: Path) -> None:
    with pytest.raises(RepositoryError, match="git merge"):
        await GitRepository().merge(str(project), "no-such-branch", "merge")


async def test_a_worktree_left_unrecorded_is_made_again(project: Path, tmp_path: Path) -> None:
    repo = GitRepository()
    path = tmp_path / "wt" / "step-1"
    await repo.add_worktree(str(project), str(path), "baton/step-1")
    (path / "stray.txt").write_text("from a run that stopped\n")
    await repo.add_worktree(str(project), str(path), "baton/step-1")
    assert names(path) == []


async def test_a_merge_left_in_conflict_is_not_committed(project: Path, tmp_path: Path) -> None:
    repo = GitRepository()
    (project / "a.txt").write_text("a\n")
    await repo.commit_all(str(project), "plan")
    one, two = tmp_path / "one", tmp_path / "two"
    await repo.add_worktree(str(project), str(one), "baton/step-1")
    await repo.add_worktree(str(project), str(two), "baton/step-2")
    (one / "a.txt").write_text("one\n")
    (two / "a.txt").write_text("two\n")
    await repo.commit_all(str(one), "1")
    await repo.commit_all(str(two), "2")
    await repo.merge(str(project), "baton/step-1", "m1")
    git(project, "merge", "baton/step-2", check=False)  # an agent starts, and gives up

    with pytest.raises(RepositoryError, match=r"unresolved .*a\.txt"):
        await repo.finish_merge(str(project), "m2")
    assert not await repo.contains(str(project), "baton/step-2")
