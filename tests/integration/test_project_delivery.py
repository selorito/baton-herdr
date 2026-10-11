"""A finished project is delivered (ADR 0016): a summary of what it changed, a link to its
page on the read-only preview, and a picture of that page."""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from baton_herdr.core.commands import complete_task
from baton_herdr.core.fakes import FixedClock, InMemoryEventStore
from baton_herdr.core.model import TaskId
from baton_herdr.core.notify import Notice, NoticeKind
from baton_herdr.core.projection import project
from baton_herdr.projects.deliver import (
    PreviewServer,
    Showcase,
    find_browser,
    find_page,
    page_url,
    screenshot,
)
from baton_herdr.projects.flow import approve_plan, deliver_projects, propose_plans, start_project
from baton_herdr.telegram.notifier import Buttons, TelegramNotifier
from baton_herdr.workdir import GitWorkspace

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
PLAN = {
    "summary": "A static page.",
    "steps": [
        {"title": "Page", "instructions": "Write index.html."},
        {"title": "Check", "instructions": "Check it."},
    ],
}


def fetch(url: str) -> int:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310 - a local test server
            return int(response.status)
    except urllib.error.HTTPError as err:
        return err.code


@pytest.fixture
def preview(tmp_path: Path) -> Iterator[PreviewServer]:
    server = PreviewServer(tmp_path / "projects", host="127.0.0.1", port=0)
    assert server.start()
    yield server
    server.stop()


def test_the_page_is_found_where_sites_keep_it(tmp_path: Path) -> None:
    assert find_page(tmp_path) is None
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "index.html").write_text("<h1>built</h1>")
    assert find_page(tmp_path) == tmp_path / "dist"
    (tmp_path / "index.html").write_text("<h1>top</h1>")
    assert find_page(tmp_path) == tmp_path  # the top of the project first
    url = page_url(tmp_path.parent, tmp_path / "dist", host="127.0.0.1", port=8765)
    assert url == f"http://127.0.0.1:8765/{tmp_path.name}/dist/"


def test_the_preview_serves_pages_and_nothing_else(preview: PreviewServer, tmp_path: Path) -> None:
    site = preview.root / "cafe-1a2b3c"
    (site / ".git").mkdir(parents=True)
    (site / ".git" / "config").write_text("[core]")
    (site / ".baton").mkdir()
    (site / ".baton" / "plan.json").write_text("{}")
    (site / ".env").write_text("TOKEN=secret")
    (site / "index.html").write_text("<h1>Cafe</h1>")
    (site / "notes").mkdir()
    (site / "notes" / "todo.txt").write_text("x")
    secret = tmp_path / "outside.txt"
    secret.write_text("not a project's")
    (site / "leak.txt").symlink_to(secret)
    base = f"http://127.0.0.1:{preview.port}"

    assert fetch(f"{base}/cafe-1a2b3c/") == 200
    assert fetch(f"{base}/cafe-1a2b3c/index.html") == 200
    assert fetch(f"{base}/cafe-1a2b3c/notes/todo.txt") == 200
    for hidden in (
        "/cafe-1a2b3c/.git/config",
        "/cafe-1a2b3c/.baton/plan.json",
        "/cafe-1a2b3c/.env",
        "/cafe-1a2b3c/%2Egit/config",
        "/",  # no listing of the projects
        "/cafe-1a2b3c/notes/",  # nor of a directory without a page
        "/cafe-1a2b3c/leak.txt",  # nor what a link points to outside them
        "/../outside.txt",
    ):
        assert fetch(base + hidden) == 404, hidden


class FakeShow:
    def __init__(self, picture: Path) -> None:
        self._picture = picture

    def url(self, workdir: Path) -> str | None:
        return f"http://127.0.0.1:8765/{workdir.name}/" if find_page(workdir) else None

    async def picture(self, url: str, workdir: Path) -> Path | None:
        del url, workdir
        return self._picture


async def finish(store: InMemoryEventStore, task_id: TaskId, clock: FixedClock) -> None:
    task = project(await store.read()).tasks[task_id]
    await store.append(complete_task(task, at=clock.now()))


async def test_a_project_is_delivered_once_its_last_step_completed(tmp_path: Path) -> None:
    store, clock = InMemoryEventStore(), FixedClock(NOW)
    _, plan_id, workdir = await start_project(
        store, root=tmp_path / "projects", request="A cafe page", clock=clock
    )
    (workdir / ".baton" / "plan.json").write_text(json.dumps(PLAN))
    await finish(store, plan_id, clock)
    await propose_plans(store, clock=clock, read_text=lambda p: p.read_text())
    first, last = await approve_plan(store, plan_id, by="cli", clock=clock)
    show = FakeShow(tmp_path / "shot.png")

    await finish(store, first, clock)
    (workdir / "index.html").write_text("<h1>Cafe</h1>\n<p>Menu</p>\n")
    assert await deliver_projects(store, clock=clock, workspace=GitWorkspace(), show=show) == []

    await finish(store, last, clock)
    (notice,) = await deliver_projects(store, clock=clock, workspace=GitWorkspace(), show=show)
    assert notice.kind is NoticeKind.PROJECT_DELIVERED
    assert not notice.urgent
    assert notice.label == "✅ Project done"
    assert notice.title == "A cafe page"
    assert notice.text == "All 2 steps are done."
    assert notice.details[0] == f"Preview: http://127.0.0.1:8765/{workdir.name}/"
    assert "index.html" in notice.blocks[0]
    assert ".baton" not in notice.blocks[0]  # the plan is baton's, not the project's
    assert notice.image == str(tmp_path / "shot.png")
    assert project(await store.read()).tasks[last].delivered
    assert await deliver_projects(store, clock=clock, workspace=GitWorkspace(), show=show) == []


async def test_a_delivery_notice_is_followed_by_its_picture(tmp_path: Path) -> None:
    sent: list[tuple[str, object]] = []

    async def send(_chat: int, text: str, _buttons: Buttons, _silent: bool) -> None:
        sent.append(("text", text))

    async def send_photo(_chat: int, path: str) -> None:
        sent.append(("photo", path))

    notifier = TelegramNotifier(send, chat_id=1, send_photo=send_photo)
    await notifier.notify(
        Notice(NoticeKind.PROJECT_DELIVERED, TaskId("t1"), "Done.", image="/p/shot.png")
    )
    assert [kind for kind, _ in sent] == ["text", "photo"]
    assert sent[1] == ("photo", "/p/shot.png")


# A real browser: a CI runner's Chrome may not start its sandbox, and baton never turns
# the sandbox off for pages agents wrote. Run with `just smoke`.
@pytest.mark.live
@pytest.mark.skipif(find_browser(None) is None, reason="no Chromium or Chrome on this machine")
async def test_the_page_is_photographed_with_a_headless_browser(
    preview: PreviewServer,
) -> None:
    site = preview.root / "cafe"
    site.mkdir(parents=True)
    (site / "index.html").write_text("<body style='background:#c33'><h1>Cafe</h1></body>")
    browser = find_browser(None)
    assert browser is not None
    url = Showcase(preview, browser).url(site)
    assert url == f"http://127.0.0.1:{preview.port}/cafe/"
    out = site / ".baton" / "screenshot.png"
    assert await screenshot(browser, url, out)
    assert (await asyncio.to_thread(out.read_bytes))[:8] == b"\x89PNG\r\n\x1a\n"
