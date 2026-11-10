"""Where the assistant may offer work in an existing directory (ADR 0021, amended): a
directory the policy trusts, or a project baton made; nothing else, and nothing new."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from baton_herdr.assistant.places import (
    LocalDirectories,
    Place,
    Places,
    RefusalReason,
    Refused,
)
from baton_herdr.core.events import StoredEvent, TaskCreated
from baton_herdr.core.fakes import FakeDirectories
from baton_herdr.core.model import TaskId

if TYPE_CHECKING:
    from pathlib import Path

NOW = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)
TRUSTED = "/home/me/dev/baton-projects"
PROJECT_DIR = "/srv/baton/p-1a2b3c-cafe"


def plan_of_a_project() -> list[StoredEvent]:
    created = TaskCreated(
        occurred_at=NOW,
        task_id=TaskId("t-1a2b3c4d"),
        title="Plan",
        instructions="-",
        workdir=PROJECT_DIR,
        project="p-1a2b3c",
        role="plan",
        request="a site for my cafe",
    )
    return [StoredEvent(seq=1, event=created)]


def places(*existing: str) -> Places:
    return Places(directories=FakeDirectories(*existing), trusted=(TRUSTED,))


def test_a_directory_inside_trusted_dirs_is_offered_with_home_expanded() -> None:
    smoke = f"{TRUSTED}/wsl1-smoke"
    found = places(smoke).find("~/dev/baton-projects/wsl1-smoke/", [])
    assert found == Place(path=smoke, by="trusted_dirs")


def test_a_project_baton_made_is_offered_by_its_directory_or_its_id() -> None:
    events = plan_of_a_project()
    by_dir = places(PROJECT_DIR).find(PROJECT_DIR, events)
    by_id = places(PROJECT_DIR).find("p-1a2b3c", events)
    assert by_dir == by_id == Place(path=PROJECT_DIR, by="p-1a2b3c")


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        ("/etc", "untrusted"),
        # Next to a trusted directory is not inside it.
        ("/home/me/dev/baton-projects-old", "untrusted"),
        (f"{TRUSTED}/not-made-yet", "missing"),
        ("wsl1-smoke", "relative"),
        ("p-ffffff", "unknown_project"),
    ],
)
def test_anything_else_is_refused_with_the_reason(path: str, reason: RefusalReason) -> None:
    existing = ("/etc", "/home/me/dev/baton-projects-old", PROJECT_DIR)
    found = places(*existing).find(path, plan_of_a_project())
    assert found == Refused(path=path, reason=reason)


def test_a_link_out_of_a_trusted_directory_is_judged_where_it_leads(tmp_path: Path) -> None:
    trusted, outside = tmp_path / "trusted", tmp_path / "outside"
    trusted.mkdir()
    outside.mkdir()
    (trusted / "link").symlink_to(outside)
    real = Places(directories=LocalDirectories(), trusted=(str(trusted),))
    assert real.find(str(trusted / "link"), []) == Refused(str(trusted / "link"), "untrusted")
    assert real.find(str(trusted), []) == Place(str(trusted), "trusted_dirs")


def test_the_real_directories_create_nothing(tmp_path: Path) -> None:
    directories = LocalDirectories()
    assert directories.resolve(str(tmp_path / "missing")) is None
    (tmp_path / "file").write_text("-", encoding="utf-8")
    assert directories.resolve(str(tmp_path / "file")) is None
    assert directories.resolve("~no-such-user-here/x") is None
    assert not (tmp_path / "missing").exists()
