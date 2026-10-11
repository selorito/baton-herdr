"""An agent's folder-trust question is answered only in a directory baton made for a
project, and only when the question names that directory (ADR 0016)."""

from __future__ import annotations

from pathlib import Path

from baton_herdr.adapters import ADAPTERS
from baton_herdr.core.model import AgentKind, AgentState
from baton_herdr.scheduler.runner import _inside, _shows_dir, _startup_keys

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
