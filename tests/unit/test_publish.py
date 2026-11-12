"""A delivered page published on Vercel (ADR 0023), against a fake of Vercel's API."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from baton_herdr.core.config import ProjectsSettings
from baton_herdr.projects.publish import (
    MAX_FILES,
    PublishError,
    VercelPublisher,
    page_files,
    public_url,
    publisher_for,
    vercel_name,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path


class FakeVercel:
    """Vercel's file upload and deployment endpoints, as far as baton uses them."""

    def __init__(
        self,
        *,
        states: tuple[str, ...] = ("BUILDING", "READY"),
        alias: tuple[str, ...] = ("cafe-1a2b3c.vercel.app", "cafe-1a2b3c-me.vercel.app"),
        refuse: int | None = None,
    ) -> None:
        self.uploads: dict[str, bytes] = {}
        self.deployments: list[dict[str, Any]] = []
        self.urls: list[str] = []
        self.tokens: set[str] = set()
        self._states = list(states)
        self._alias = list(alias)
        self._refuse = refuse

    def __call__(
        self, method: str, url: str, headers: Mapping[str, str], body: bytes | None
    ) -> tuple[int, bytes]:
        self.urls.append(f"{method} {url}")
        self.tokens.add(headers["Authorization"])
        if self._refuse is not None:
            error = {"error": {"code": "forbidden", "message": "Not authorized"}}
            return self._refuse, json.dumps(error).encode()
        if url.startswith("https://api.vercel.com/v2/files"):
            assert body is not None
            assert headers["x-vercel-digest"] == hashlib.sha1(body).hexdigest()  # noqa: S324
            self.uploads[headers["x-vercel-digest"]] = body
            return 200, b"{}"
        if method == "POST" and "/v13/deployments" in url:
            assert body is not None
            deployment = json.loads(body)
            assert all(f["sha"] in self.uploads for f in deployment["files"])
            self.deployments.append(deployment)
            return 200, json.dumps({"id": "dpl_1", "url": "cafe-xyz.vercel.app"}).encode()
        if method == "GET" and "/v13/deployments/dpl_1" in url:
            state = self._states.pop(0) if len(self._states) > 1 else self._states[0]
            answer: dict[str, Any] = {"id": "dpl_1", "readyState": state}
            answer["url"] = "cafe-xyz.vercel.app"
            if state == "READY":
                answer["alias"] = self._alias
            return 200, json.dumps(answer).encode()
        return 404, b'{"error": {"code": "not_found"}}'


async def no_sleep(_seconds: float) -> None:
    return None


def site(root: Path, name: str = "cafe-1a2b3c") -> Path:
    workdir = root / name
    (workdir / ".git").mkdir(parents=True)
    (workdir / ".git" / "config").write_text("[core]")
    (workdir / ".baton").mkdir()
    (workdir / ".baton" / "plan.json").write_text("{}")
    (workdir / ".env").write_text("TOKEN=secret")
    (workdir / "index.html").write_text("<h1>Cafe</h1>")
    (workdir / "css").mkdir()
    (workdir / "css" / "site.css").write_text("h1 { color: red }")
    return workdir


async def test_a_page_is_deployed_to_production_and_its_public_link_returned(
    tmp_path: Path,
) -> None:
    vercel = FakeVercel()
    publisher = VercelPublisher("tok", http=vercel, sleep=no_sleep)

    url = await publisher.publish(site(tmp_path))

    assert url == "https://cafe-1a2b3c.vercel.app"  # the project's own domain: public
    (deployment,) = vercel.deployments
    assert deployment["name"] == "cafe-1a2b3c"
    assert deployment["target"] == "production"
    assert deployment["projectSettings"]["framework"] is None
    assert sorted(f["file"] for f in deployment["files"]) == ["css/site.css", "index.html"]
    assert vercel.tokens == {"Bearer tok"}


async def test_secrets_and_repositories_are_never_sent(tmp_path: Path) -> None:
    workdir = site(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("not the page's")
    (workdir / "leak.txt").symlink_to(outside)
    vercel = FakeVercel()

    await VercelPublisher("tok", http=vercel, sleep=no_sleep).publish(workdir)

    sent = b"".join(vercel.uploads.values())
    assert b"secret" not in sent
    assert b"[core]" not in sent
    assert b"not the page's" not in sent


async def test_a_built_page_is_published_from_where_it_was_built(tmp_path: Path) -> None:
    workdir = tmp_path / "app-9f"
    (workdir / "dist").mkdir(parents=True)
    (workdir / "dist" / "index.html").write_text("<h1>built</h1>")
    (workdir / "package.json").write_text("{}")
    vercel = FakeVercel()

    await VercelPublisher("tok", http=vercel, sleep=no_sleep).publish(workdir)

    assert [f["file"] for f in vercel.deployments[0]["files"]] == ["index.html"]


async def test_a_project_without_a_page_is_refused(tmp_path: Path) -> None:
    (tmp_path / "lib").mkdir()
    with pytest.raises(PublishError, match="no page"):
        await VercelPublisher("tok", http=FakeVercel(), sleep=no_sleep).publish(tmp_path)


async def test_a_refused_token_says_so(tmp_path: Path) -> None:
    publisher = VercelPublisher("bad", http=FakeVercel(refuse=403), sleep=no_sleep)
    with pytest.raises(PublishError, match=r"403: Not authorized"):
        await publisher.publish(site(tmp_path))


async def test_a_failed_deployment_says_so(tmp_path: Path) -> None:
    publisher = VercelPublisher("tok", http=FakeVercel(states=("ERROR",)), sleep=no_sleep)
    with pytest.raises(PublishError, match="ERROR"):
        await publisher.publish(site(tmp_path))


async def test_a_deployment_that_never_gets_ready_is_given_up(tmp_path: Path) -> None:
    publisher = VercelPublisher(
        "tok", http=FakeVercel(states=("BUILDING",)), sleep=no_sleep, ready_timeout_s=10
    )
    with pytest.raises(PublishError, match="not ready"):
        await publisher.publish(site(tmp_path))


async def test_a_team_is_named_on_every_request(tmp_path: Path) -> None:
    vercel = FakeVercel()
    await VercelPublisher("tok", team="team_abc", http=vercel, sleep=no_sleep).publish(
        site(tmp_path)
    )
    assert all("teamId=team_abc" in url for url in vercel.urls)


def test_a_page_too_large_is_refused(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("x")
    for index in range(MAX_FILES + 1):
        (tmp_path / f"{index}.txt").write_text("x")
    with pytest.raises(PublishError, match="too large"):
        page_files(tmp_path)


@pytest.mark.parametrize(
    "directory", ["cafe-1a2b3c", "Bana_Berranur---site", "ğüş-çay", "x" * 150, "---", ".x."]
)
def test_vercel_names_follow_vercel_s_rules(directory: str) -> None:
    result = vercel_name(directory)
    assert result == result.lower()
    assert len(result) <= 100
    assert "---" not in result
    assert all(c.isascii() and (c.isalnum() or c in "._-") for c in result)
    assert result[0] not in "-._"
    assert result[-1] not in "-._"


def test_a_slug_is_kept_as_the_name() -> None:
    assert vercel_name("bana-berranur-yuceaktas-seni-cok-d20f0e") == (
        "bana-berranur-yuceaktas-seni-cok-d20f0e"
    )


def test_the_shortest_alias_is_the_public_link() -> None:
    deployment = {"alias": ["cafe-me.vercel.app", "cafe.vercel.app"], "url": "cafe-xyz.vercel.app"}
    assert public_url(deployment) == "https://cafe.vercel.app"
    assert public_url({"url": "cafe-xyz.vercel.app"}) == "https://cafe-xyz.vercel.app"


def test_publishing_is_off_unless_asked_for_with_a_token() -> None:
    assert publisher_for(ProjectsSettings()) is None
    assert publisher_for(ProjectsSettings(publish="vercel")) is None
    assert publisher_for(ProjectsSettings(vercel_token=SecretStr("t"))) is None
    on = ProjectsSettings(publish="vercel", vercel_token=SecretStr("t"))
    assert isinstance(publisher_for(on), VercelPublisher)
