"""A finished project's page: where it is, a read-only preview of it, and a screenshot of
that preview (ADR 0016).

The preview serves the projects root over HTTP, for reading only. It answers only for
files: no directory listings, and nothing under a hidden name (``.git``, ``.baton``,
``.env``), so the repositories and their secrets are never served. A directory is
served by its ``index.html``.
"""

from __future__ import annotations

import asyncio
import shutil
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote, unquote, urlsplit

from baton_herdr.core.logging import get_logger

if TYPE_CHECKING:
    import os
    from typing import Any

# Where a project's page may be, in this order.
PAGE_DIRS = ("", "dist", "build", "public", "site")
SCREENSHOT_TIMEOUT_S = 45
SCREENSHOT_FILE = ".baton/screenshot.png"


def find_page(workdir: Path) -> Path | None:
    """The directory holding the project's ``index.html``, if it has one."""
    for name in PAGE_DIRS:
        directory = workdir / name if name else workdir
        if (directory / "index.html").is_file():
            return directory
    return None


def page_url(root: Path, page: Path, *, host: str, port: int) -> str:
    relative = page.resolve().relative_to(root.resolve()).as_posix()
    return f"http://{host}:{port}/{quote(relative)}/"


def find_browser(configured: str | None) -> Path | None:
    """A Chromium or Chrome: the configured one, else one on PATH, else Playwright's. A snap
    package comes last: it has a /tmp of its own, so it can only write under the home
    directory (where projects are, but not every caller's files)."""
    if configured:
        path = Path(configured).expanduser()
        found = path if path.is_file() else shutil.which(configured)
        return Path(found) if found else None
    on_path = [
        Path(found)
        for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable")
        if (found := shutil.which(name))
    ]
    cache = Path.home() / ".cache" / "ms-playwright"
    playwright = sorted(cache.glob("chromium-*/chrome-linux/chrome"))[-1:]
    snaps = [p for p in on_path if _is_snap(p)]
    ordered = [*(p for p in on_path if p not in snaps), *playwright, *snaps]
    return ordered[0] if ordered else None


def _is_snap(path: Path) -> bool:
    """A snap's command (a link to /usr/bin/snap) or a script that starts one (Ubuntu's
    /usr/bin/chromium-browser)."""
    if path.resolve().name == "snap" or path.resolve().is_relative_to("/snap"):
        return True
    try:
        with path.open("rb") as file:
            head = file.read(4096)
    except OSError:
        return False
    return head.startswith(b"#!") and b"/snap/" in head


async def screenshot(browser: Path, url: str, out: Path) -> bool:
    """Save ``url`` as it looks at 1280x900 in ``out``; False when that did not work."""
    await asyncio.to_thread(_clear, out)
    try:
        process = await asyncio.create_subprocess_exec(
            str(browser),
            "--headless=new",
            "--disable-gpu",
            "--hide-scrollbars",
            "--window-size=1280,900",
            f"--screenshot={out}",
            url,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(process.wait(), SCREENSHOT_TIMEOUT_S)
    except (OSError, TimeoutError) as err:
        get_logger("baton.projects").warning("screenshot failed", url=url, error=repr(err))
        return False
    return await asyncio.to_thread(_written, out)


def _clear(out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.unlink(missing_ok=True)


def _written(out: Path) -> bool:
    return out.is_file() and out.stat().st_size > 0


class _PreviewHandler(SimpleHTTPRequestHandler):
    """Files only, and none under a hidden name."""

    def send_head(self) -> Any:
        path = unquote(urlsplit(self.path).path)
        if any(part.startswith(".") for part in path.split("/") if part):
            self.send_error(404)
            return None
        local = Path(self.translate_path(self.path))
        root = Path(self.directory).resolve()
        if not local.resolve().is_relative_to(root):
            self.send_error(404)  # a link an agent made, pointing out of the projects
            return None
        if local.is_dir() and not (local / "index.html").is_file():
            self.send_error(404)
            return None
        return super().send_head()

    def list_directory(self, path: str | os.PathLike[str]) -> None:
        del path
        self.send_error(404)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - base signature
        del format, args  # batond's log is not an access log


class Showcase:
    """What a delivery shows of a project: its page's link and a picture of it."""

    def __init__(self, server: PreviewServer | None, browser: Path | None) -> None:
        self._server = server
        self._browser = browser

    def url(self, workdir: Path) -> str | None:
        page = find_page(workdir)
        if page is None or self._server is None or not self._server.running:
            return None
        return page_url(self._server.root, page, host=self._server.host, port=self._server.port)

    async def picture(self, url: str, workdir: Path) -> Path | None:
        if self._browser is None:
            return None
        out = workdir / SCREENSHOT_FILE
        return out if await screenshot(self._browser, url, out) else None


class PreviewServer:
    """The read-only preview, in a thread of batond's process."""

    def __init__(self, root: Path, *, host: str, port: int) -> None:
        self.root = root
        self.host = host
        self.port = port
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> bool:
        """Start serving; False (logged) when the address cannot be used."""
        handler = partial(_PreviewHandler, directory=str(self.root))
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            self._server = ThreadingHTTPServer((self.host, self.port), handler)
        except OSError as err:
            get_logger("baton.projects").warning(
                "project preview not started", host=self.host, port=self.port, error=repr(err)
            )
            return False
        self.port = self._server.server_address[1]  # the real one, when 0 was asked for
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="baton-preview", daemon=True
        )
        self._thread.start()
        return True

    @property
    def running(self) -> bool:
        return self._server is not None

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
