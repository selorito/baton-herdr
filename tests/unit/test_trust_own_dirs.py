"""An agent's folder-trust question is answered only in a directory baton made for a
project, and only when the question names that directory (ADR 0016)."""

from __future__ import annotations

from pathlib import Path

from baton_herdr.adapters import ADAPTERS
from baton_herdr.core.model import AgentKind, AgentState, TaskId
from baton_herdr.core.projection import TaskView
from baton_herdr.scheduler.runner import _inside, _shows_dir, _startup_keys, _trusts

FIXTURES = Path(__file__).parents[2] / "fixtures"
CLAUDE = ADAPTERS[AgentKind.CLAUDE]
TRUST = "baton:claude_trust_folder"


def trust_screen(directory: str) -> str:
    recorded = FIXTURES / "claude/blocked_permission/20261011T113734Z/screen.detection.txt"
    return recorded.read_text().replace("~/dev/baton-sandbox", directory)


def test_the_question_is_answered_in_the_projects_own_directory() -> None:
    own = "/home/u/dev/baton-projects/cafe-1a2b3c"
    keys = _startup_keys(CLAUDE, AgentState.BLOCKED_OTHER, TRUST, trust_screen(own), own)
    assert keys == ("Down", "Enter")


def test_the_question_goes_to_a_person_anywhere_else() -> None:
    own = "/home/u/dev/baton-projects/cafe-1a2b3c"
    elsewhere = trust_screen("/home/u/dev/somebody-elses-repo")
    assert _startup_keys(CLAUDE, AgentState.BLOCKED_OTHER, TRUST, elsewhere, own) is None
    # Not a project of baton's: no directory to trust.
    assert _startup_keys(CLAUDE, AgentState.BLOCKED_OTHER, TRUST, trust_screen(own), None) is None
    # Another kind of blocker in the project's directory stays with a person.
    assert (
        _startup_keys(CLAUDE, AgentState.BLOCKED_PERMISSION, TRUST, trust_screen(own), own) is None
    )


def test_a_directory_is_recognised_wrapped_or_under_home() -> None:
    own = str(Path.home() / "dev" / "baton-projects" / "cafe-1a2b3c")
    assert _shows_dir(" Accessing workspace:\n ~/dev/baton-projects/cafe-1a2b\n3c\n", own)
    assert not _shows_dir(" ~/dev/baton-projects/cafe-9f9f9f\n", own)


def test_only_directories_under_the_projects_root_are_baton_s(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    (root / "cafe").mkdir(parents=True)
    assert _inside(str(root / "cafe"), [str(root)])
    assert not _inside(str(tmp_path / "elsewhere"), [str(root)])
    assert not _inside(str(root / ".." / "elsewhere"), [str(root)])
    assert not _inside(str(root / "cafe"), [])


def test_a_project_s_own_directory_or_a_trusted_one_is_trusted(tmp_path: Path) -> None:
    root, trusted = tmp_path / "projects", tmp_path / "work"
    (root / "cafe").mkdir(parents=True)
    (trusted / "repo").mkdir(parents=True)

    def task(workdir: Path, project: str | None = None) -> TaskView:
        return TaskView(task_id=TaskId("t"), title="t", workdir=str(workdir), project=project)

    own, policy = [str(root)], [str(trusted)]
    assert _trusts(task(root / "cafe", "p-1"), own, policy)
    assert not _trusts(task(root / "cafe"), own, policy)  # not made by baton for a project
    assert _trusts(task(trusted / "repo"), own, policy)  # the operator trusts it
    assert not _trusts(task(tmp_path / "elsewhere"), own, policy)
