"""A delivered project's page, published where anyone can open it: Vercel (ADR 0023).

The preview (``deliver.PreviewServer``) serves pages on this machine's networks only. With
``[projects] publish = "vercel"`` a delivered project's page is also deployed to Vercel,
as a static site of its own, and the delivery notice carries its public link.

Only the page's directory is sent, by the rules the preview serves by: files only, none
under a hidden name (``.git``, ``.baton``, ``.env``), none a link points to outside the
directory. Vercel's REST API is used directly: each file is uploaded by its SHA-1, then a
production deployment names them. Nothing is built; a page is served as it is.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from baton_herdr.core.logging import get_logger
from baton_herdr.projects.deliver import find_page

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping
    from pathlib import Path

    from baton_herdr.core.config import ProjectsSettings

API = "https://api.vercel.com"
# Vercel's own limits are higher; a page past these is not a page.
MAX_FILES = 2000
MAX_BYTES = 50 * 1024 * 1024
READY_TIMEOUT_S = 180.0
POLL_INTERVAL_S = 2.0
HTTP_TIMEOUT_S = 60.0
# Vercel project names: lowercase letters, digits, '.', '_' and '-', at most 100, no '---'.
_NAME_LIMIT = 100


class PublishError(Exception):
    """The page could not be published; the message says why, for the notice."""


@dataclass(frozen=True, slots=True)
class PageFile:
    path: str  # relative, with '/'
    data: bytes

    @property
    def sha(self) -> str:
        return hashlib.sha1(self.data, usedforsecurity=False).hexdigest()


def page_files(page: Path) -> list[PageFile]:
    """The files of ``page`` the preview would serve, by relative path."""
    root = page.resolve()
    files: list[PageFile] = []
    total = 0
    for path in sorted(page.rglob("*")):
        relative = path.relative_to(page)
        if any(part.startswith(".") for part in relative.parts):
            continue
        if not path.is_file() or not path.resolve().is_relative_to(root):
            continue
        data = path.read_bytes()
        total += len(data)
        files.append(PageFile(relative.as_posix(), data))
        if len(files) > MAX_FILES or total > MAX_BYTES:
            msg = f"the page is too large to publish (over {MAX_FILES} files or 50 MB)"
            raise PublishError(msg)
    if not any(f.path == "index.html" for f in files):
        raise PublishError("the page has no index.html")
    return files


def vercel_name(directory: str) -> str:
    """A Vercel project name for a project's directory name (already a slug)."""
    name = re.sub(r"[^a-z0-9._-]+", "-", directory.lower())
    name = re.sub(r"-{3,}", "--", name).strip("-._")[:_NAME_LIMIT].strip("-._")
    return name or "baton-project"


def urllib_http(
    method: str, url: str, headers: Mapping[str, str], body: bytes | None
) -> tuple[int, bytes]:
    """One HTTP request: (status, body). Blocking; run off the event loop."""
    request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)  # noqa: S310 - https only, the URL is API's
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_S) as response:  # noqa: S310
            return int(response.status), response.read()
    except urllib.error.HTTPError as err:
        return err.code, err.read()
    except (urllib.error.URLError, TimeoutError, OSError) as err:
        msg = f"Vercel could not be reached: {err}"
        raise PublishError(msg) from err


class VercelPublisher:
    """Deploys a project's page to Vercel as a production deployment of its own project."""

    def __init__(
        self,
        token: str,
        *,
        team: str | None = None,
        http: Callable[[str, str, Mapping[str, str], bytes | None], tuple[int, bytes]] = (
            urllib_http
        ),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        ready_timeout_s: float = READY_TIMEOUT_S,
    ) -> None:
        self._token = token
        self._team = team
        self._http = http
        self._sleep = sleep
        self._ready_timeout_s = ready_timeout_s

    async def publish(self, workdir: Path) -> str:
        """The page's public link. Raises :class:`PublishError`."""
        page = find_page(workdir)
        if page is None:
            raise PublishError("the project has no page (index.html)")
        files = await asyncio.to_thread(page_files, page)
        for file in files:
            await self._upload(file)
        deployment = await self._call(
            "POST",
            "/v13/deployments",
            {
                "name": vercel_name(workdir.name),
                "target": "production",
                "files": [{"file": f.path, "sha": f.sha, "size": len(f.data)} for f in files],
                "projectSettings": {
                    "framework": None,
                    "buildCommand": None,
                    "installCommand": None,
                    "outputDirectory": None,
                    "devCommand": None,
                },
            },
            query={"skipAutoDetectionConfirmation": "1"},
        )
        ready = await self._wait_ready(str(deployment["id"]))
        url = public_url(ready)
        get_logger("baton.projects").info("published", workdir=str(workdir), url=url)
        return url

    async def _upload(self, file: PageFile) -> None:
        headers = {
            **self._auth(),
            "Content-Type": "application/octet-stream",
            "x-vercel-digest": file.sha,
        }
        status, body = await asyncio.to_thread(
            self._http, "POST", self._url("/v2/files"), headers, file.data
        )
        if status >= 300:
            raise PublishError(_failure(f"upload of {file.path}", status, body))

    async def _wait_ready(self, deployment_id: str) -> Mapping[str, Any]:
        waited = 0.0
        while True:
            deployment = await self._call("GET", f"/v13/deployments/{deployment_id}")
            state = deployment.get("readyState") or deployment.get("status")
            if state == "READY" and (deployment.get("alias") or waited >= self._ready_timeout_s):
                return deployment
            if state in {"ERROR", "CANCELED"}:
                msg = f"Vercel's deployment ended in {state}"
                raise PublishError(msg)
            if waited >= self._ready_timeout_s:
                msg = f"Vercel's deployment was not ready after {self._ready_timeout_s:.0f} s"
                raise PublishError(msg)
            await self._sleep(POLL_INTERVAL_S)
            waited += POLL_INTERVAL_S

    async def _call(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None = None,
        *,
        query: Mapping[str, str] | None = None,
    ) -> Mapping[str, Any]:
        body = json.dumps(payload).encode() if payload is not None else None
        headers = {**self._auth(), "Content-Type": "application/json"}
        status, raw = await asyncio.to_thread(
            self._http, method, self._url(path, query), headers, body
        )
        if status >= 300:
            raise PublishError(_failure(f"{method} {path}", status, raw))
        try:
            answer: Mapping[str, Any] = json.loads(raw)
        except ValueError as err:
            msg = f"Vercel answered {method} {path} with something not JSON"
            raise PublishError(msg) from err
        return answer

    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    def _url(self, path: str, query: Mapping[str, str] | None = None) -> str:
        params = dict(query or {})
        if self._team:
            params["teamId" if self._team.startswith("team_") else "slug"] = self._team
        suffix = "&".join(f"{k}={urllib.parse.quote(v)}" for k, v in params.items())
        return f"{API}{path}" + (f"?{suffix}" if suffix else "")


def public_url(deployment: Mapping[str, Any]) -> str:
    """The link anyone can open: the shortest production alias (the project's own
    ``<name>.vercel.app``). A deployment's own URL is behind Vercel's login by default."""
    aliases = [a for a in deployment.get("alias") or [] if isinstance(a, str)]
    host = min(aliases, key=len) if aliases else str(deployment.get("url", ""))
    return f"https://{host}"


def _failure(what: str, status: int, body: bytes) -> str:
    try:
        error = json.loads(body).get("error") or {}
        reason = error.get("message") or error.get("code") or ""
    except (ValueError, AttributeError):
        reason = ""
    if status in {401, 403}:
        reason = reason or "the token was refused"
    return f"Vercel refused the {what} ({status}{': ' + reason if reason else ''})"


def publisher_for(settings: ProjectsSettings) -> VercelPublisher | None:
    """The publisher ``[projects] publish`` names; None when off or without a token."""
    if settings.publish != "vercel" or settings.vercel_token is None:
        return None
    return VercelPublisher(settings.vercel_token.get_secret_value(), team=settings.vercel_team)
