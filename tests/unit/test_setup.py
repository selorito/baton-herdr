"""baton setup: the files it writes are ones baton reads, Telegram's owner is found from a
message, secrets are never shown and kept private, and running it again keeps what is
there."""

from __future__ import annotations

import hashlib
import stat
import tomllib
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from baton_herdr.core.config import BatonSettings
from baton_herdr.policy.load import load_policy
from baton_herdr.setup import (
    Io,
    Owner,
    Paths,
    SetupError,
    find_owner,
    first_config,
    first_policy,
    run_setup,
    set_env_value,
    set_toml_value,
)

from platform_marks import posix_only

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

TOKEN = "123456:ABC-not-a-real-token"  # noqa: S105 - a test value


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("[telegram]\nchat_id = 1\n", "[telegram]\nchat_id = 42\n"),
        ("[telegram]\n# chat_id = 123456789\n", "[telegram]\nchat_id = 42\n"),
        (
            "[telegram]\n# a note\nowner_id = 7\n",
            "[telegram]\nchat_id = 42\n# a note\nowner_id = 7\n",
        ),
        ("[scheduler]\nagents = []\n", "[scheduler]\nagents = []\n\n[telegram]\nchat_id = 42\n"),
        ("", "[telegram]\nchat_id = 42\n"),
        (
            "[telegram]\n[policy]\nchat_id = 1\n",  # another section's key is not touched
            "[telegram]\nchat_id = 42\n[policy]\nchat_id = 1\n",
        ),
    ],
)
def test_a_toml_value_is_set_in_its_section_and_nothing_else_moves(
    text: str, expected: str
) -> None:
    assert set_toml_value(text, "telegram", "chat_id", "42") == expected


def test_the_first_files_are_ones_baton_reads(tmp_path: Path) -> None:
    config = tomllib.loads(first_config(["claude", "codex"], "Europe/Istanbul", phone_preview=True))
    settings = BatonSettings.model_validate(config)
    assert settings.scheduler.agents == ("claude", "codex")
    assert settings.scheduler.timezone == "Europe/Istanbul"
    assert settings.projects.preview_host == "0.0.0.0"  # noqa: S104 - asked for in the test

    policy = tmp_path / "policy.yaml"
    policy.write_text(first_policy([str(tmp_path / "repo")]))
    assert load_policy(policy).trusts(str(tmp_path / "repo" / "src"))
    policy.write_text(first_policy([]))
    assert load_policy(policy).trusted_dirs == ()


def test_the_owner_is_the_newest_private_message() -> None:
    updates = {
        "result": [
            {"update_id": 1, "message": {"chat": {"id": -5, "type": "group"}, "from": {"id": 9}}},
            {
                "update_id": 2,
                "message": {
                    "chat": {"id": 77, "type": "private"},
                    "from": {"id": 77, "username": "lab"},
                },
            },
            {"update_id": 3, "edited_message": {"chat": {"id": 1, "type": "private"}}},
        ]
    }
    assert find_owner(updates) == Owner(chat_id=77, user_id=77, name="lab")
    assert find_owner({"result": [updates["result"][0]]}) is None  # a group never


def test_env_values_are_set_without_losing_the_others() -> None:
    assert set_env_value(
        "A=1\nBATON_TELEGRAM__BOT_TOKEN=old\n", "BATON_TELEGRAM__BOT_TOKEN", "new"
    ) == ("A=1\nBATON_TELEGRAM__BOT_TOKEN=new\n")


class Script:
    """A person at the terminal and Telegram, for a whole setup."""

    def __init__(
        self, *, found: Sequence[str] = ("herdr", "claude", "codex", "baton-detect")
    ) -> None:
        self.found = set(found)
        self.said: list[str] = []
        self.ran: list[list[str]] = []
        self.answers: dict[str, Any] = {}

    def io(self) -> Io:
        def confirm(question: str, *, default: bool) -> bool:
            for start, answer in self.answers.items():
                if question.startswith(start):
                    return bool(answer)
            return default

        def run(command: Sequence[str]) -> tuple[int, str]:
            self.ran.append(list(command))
            if command[:3] == ["herdr", "integration", "status"]:
                return 0, "claude: current (v10) (x)\ncodex: not installed (y)\n"
            return 0, ""

        def telegram(token: str, method: str, **_: object) -> Any:
            assert token == TOKEN
            if method == "getMe":
                return {"username": "lab_baton_bot"}
            return [
                {
                    "update_id": 10,
                    "message": {
                        "chat": {"id": 555, "type": "private"},
                        "from": {"id": 555, "first_name": "Ayşe"},
                    },
                }
            ]

        return Io(
            say=self.said.append,
            ask=lambda _question, default: self.answers.get("dirs", default),
            confirm=confirm,
            secret=lambda _question: TOKEN,
            run=run,
            which=lambda name: f"/usr/bin/{name}" if name in self.found else None,
            telegram=telegram,
            sleep=lambda _: None,
        )


def paths(tmp_path: Path) -> Paths:
    return Paths(
        config=tmp_path / "baton" / "baton.toml",
        env=tmp_path / "baton" / ".env",
        policy=tmp_path / "baton" / "policy.yaml",
        projects_root=tmp_path / "projects",
        local_bin=tmp_path / "bin",
    )


@posix_only("file modes (600 for .env, 755 for the binary)")
def test_a_fresh_machine_is_set_up_in_one_pass(tmp_path: Path) -> None:
    script = Script()
    script.answers["dirs"] = f"{tmp_path}/repo"
    where = paths(tmp_path)
    assert run_setup(
        script.io(), where, timezone="Europe/Istanbul", target="x86_64-unknown-linux-gnu"
    )

    settings = BatonSettings.model_validate(tomllib.loads(where.config.read_text()))
    assert settings.scheduler.agents == ("claude", "codex")
    assert (settings.telegram.chat_id, settings.telegram.owner_id) == (555, 555)
    assert stat.S_IMODE(where.env.stat().st_mode) == 0o600
    assert f"BATON_TELEGRAM__BOT_TOKEN={TOKEN}" in where.env.read_text()
    assert not any(TOKEN in line for line in script.said)  # never shown
    assert ["herdr", "integration", "install", "codex"] in script.ran  # claude was current
    assert ["herdr", "integration", "install", "claude"] not in script.ran
    policy = load_policy(where.policy)
    assert policy.trusts(str(tmp_path / "repo"))
    assert policy.trusts(str(tmp_path / "projects" / "x"))


def test_running_it_again_keeps_what_is_there(tmp_path: Path) -> None:
    where = paths(tmp_path)
    run_setup(Script().io(), where, timezone="UTC", target="x")
    config, policy = where.config.read_text(), where.policy.read_text()

    again = Script()
    assert run_setup(again.io(), where, timezone="Asia/Tokyo", target="x")
    assert where.config.read_text() == config  # the same owner found: nothing changed
    assert where.policy.read_text() == policy
    assert any("Keeping" in line for line in again.said)


def test_what_cannot_be_done_is_said() -> None:
    with pytest.raises(SetupError, match="herdr is not installed"):
        run_setup(Script(found=()).io(), paths(Path("/x")), timezone="UTC", target="x")
    with pytest.raises(SetupError, match="No agent"):
        run_setup(Script(found=("herdr",)).io(), paths(Path("/x")), timezone="UTC", target="x")


BINARY = b"\x7fELF baton-detect"
SUMS = (
    f"{hashlib.sha256(BINARY).hexdigest()}  baton-detect-x86_64-unknown-linux-gnu\n"
    f"{'0' * 64}  baton-detect-aarch64-unknown-linux-gnu\n"
)


def downloads(urls: list[str], *, binary: bytes = BINARY) -> Callable[[str], bytes]:
    def download(url: str) -> bytes:
        urls.append(url)
        return SUMS.encode() if url.endswith("/SHA256SUMS") else binary

    return download


@posix_only("file modes (600 for .env, 755 for the binary)")
def test_a_missing_baton_detect_is_downloaded_from_this_version_and_checked(
    tmp_path: Path,
) -> None:
    script = Script(found=("herdr", "claude"))
    where = paths(tmp_path)
    urls: list[str] = []
    io = replace(script.io(), download=downloads(urls))
    run_setup(io, where, timezone="UTC", target="x86_64-unknown-linux-gnu", version="1.4.0")
    release = "https://github.com/selorito/baton-herdr/releases/download/v1.4.0/"
    assert urls == [f"{release}SHA256SUMS", f"{release}baton-detect-x86_64-unknown-linux-gnu"]
    assert (where.local_bin / "baton-detect").read_bytes() == BINARY
    assert (where.local_bin / "baton-detect").stat().st_mode & stat.S_IXUSR
    assert not any(command[0] == "curl" for command in script.ran)


def test_a_download_that_does_not_match_its_checksum_is_not_kept(tmp_path: Path) -> None:
    script = Script(found=("herdr", "claude"))
    where = paths(tmp_path)
    io = replace(script.io(), download=downloads([], binary=b"something else"))
    assert not run_setup(io, where, timezone="UTC", target="x86_64-unknown-linux-gnu")
    assert not (where.local_bin / "baton-detect").exists()
    assert any("does not match its checksum" in line for line in script.said)


def test_a_system_without_a_released_binary_is_told_how_to_build_one(tmp_path: Path) -> None:
    script = Script(found=("herdr", "claude"))
    assert not run_setup(script.io(), paths(tmp_path), timezone="UTC", target=None)
    assert any("cargo install" in line for line in script.said)


def test_windows_gets_an_exe(tmp_path: Path) -> None:
    script = Script(found=("herdr", "claude"))
    where = paths(tmp_path)
    urls: list[str] = []
    sums = f"{hashlib.sha256(BINARY).hexdigest()}  baton-detect-x86_64-pc-windows-msvc.exe\n"

    def download(url: str) -> bytes:
        urls.append(url)
        return sums.encode() if url.endswith("/SHA256SUMS") else BINARY

    io = replace(script.io(), download=download)
    run_setup(io, where, timezone="UTC", target="x86_64-pc-windows-msvc", version="1.4.0")
    assert urls[-1].endswith("/baton-detect-x86_64-pc-windows-msvc.exe")
    assert (where.local_bin / "baton-detect.exe").read_bytes() == BINARY


def test_without_ollama_the_assistant_is_only_mentioned(tmp_path: Path) -> None:
    script = Script()
    where = paths(tmp_path)
    run_setup(script.io(), where, timezone="UTC", target="x")
    assert any("install Ollama" in line for line in script.said)
    assert not BatonSettings.model_validate(
        tomllib.loads(where.config.read_text())
    ).assistant.enabled


def test_with_ollama_the_assistant_is_set_up_when_asked(tmp_path: Path) -> None:
    script = Script(found=("herdr", "claude", "baton-detect", "ollama"))
    script.answers["Let the Telegram bot read plain messages"] = True
    where = paths(tmp_path)
    run_setup(script.io(), where, timezone="UTC", target="x")
    assert ["ollama", "pull", "qwen3.5:4b"] in script.ran  # not listed by `ollama list`
    assistant = BatonSettings.model_validate(tomllib.loads(where.config.read_text())).assistant
    assert (assistant.backend, assistant.model) == ("ollama", "qwen3.5:4b")

    again = Script(found=("herdr", "claude", "baton-detect", "ollama"))
    run_setup(again.io(), where, timezone="UTC", target="x")
    assert "Keeping the assistant's settings." in again.said
    assert ["ollama", "pull", "qwen3.5:4b"] not in again.ran


def test_the_assistant_is_off_unless_the_user_says_yes(tmp_path: Path) -> None:
    script = Script(found=("herdr", "claude", "baton-detect", "ollama"))
    where = paths(tmp_path)
    run_setup(script.io(), where, timezone="UTC", target="x")
    assert ["ollama", "list"] not in script.ran
    assert not BatonSettings.model_validate(
        tomllib.loads(where.config.read_text())
    ).assistant.enabled
