"""What differs between the systems baton runs on, decided in one place."""

from __future__ import annotations

import shlex
import threading
from pathlib import Path

import pytest

from baton_herdr.core.platform import (
    current_system,
    exclusive_lock,
    executable_name,
    herdr_config_dir,
    machine,
    pipe_name,
    release_target,
    running_on_wsl,
    running_on_wsl1,
    shell_join,
    shell_quote,
)


@pytest.mark.parametrize(
    ("name", "expected"), [("linux", "linux"), ("darwin", "macos"), ("win32", "windows")]
)
def test_the_system_is_named_by_baton_s_names(name: str, expected: str) -> None:
    assert current_system(name) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("x86_64", "x86_64"), ("AMD64", "x86_64"), ("arm64", "aarch64"), ("aarch64", "aarch64")],
)
def test_processors_have_one_name_each(raw: str, expected: str) -> None:
    assert machine(raw) == expected


@pytest.mark.parametrize(
    ("system", "processor", "target"),
    [
        ("linux", "x86_64", "x86_64-unknown-linux-gnu"),
        ("linux", "aarch64", "aarch64-unknown-linux-gnu"),
        ("macos", "aarch64", "aarch64-apple-darwin"),
        ("macos", "x86_64", "x86_64-apple-darwin"),
        ("windows", "x86_64", "x86_64-pc-windows-msvc"),
        ("windows", "aarch64", None),
        ("linux", "riscv64", None),
    ],
)
def test_each_system_downloads_its_own_binary(
    system: str, processor: str, target: str | None
) -> None:
    assert release_target(system, processor) == target  # type: ignore[arg-type]


def test_windows_programs_end_in_exe() -> None:
    assert executable_name("baton-detect", "windows") == "baton-detect.exe"
    assert executable_name("baton-detect", "linux") == "baton-detect"


@pytest.mark.parametrize(
    ("release", "wsl", "wsl1"),
    [
        ("4.4.0-26100-Microsoft", True, True),
        ("6.6.87.2-microsoft-standard-WSL2", True, False),
        ("6.8.0-138-generic", False, False),
    ],
)
def test_wsl_and_its_version_are_read_from_the_kernel(release: str, wsl: bool, wsl1: bool) -> None:
    assert running_on_wsl(release) is wsl
    assert running_on_wsl1(release) is wsl1


def test_herdr_keeps_its_sockets_in_appdata_on_windows() -> None:
    home = Path("/home/me")
    assert herdr_config_dir({}, home, "linux") == home / ".config" / "herdr"
    assert herdr_config_dir({"XDG_CONFIG_HOME": "/x"}, home, "macos") == Path("/x/herdr")
    appdata = r"C:\Users\me\AppData\Roaming"
    assert herdr_config_dir({"APPDATA": appdata}, home, "windows") == Path(appdata) / "herdr"


def test_herdr_s_windows_pipe_is_named_after_its_socket_path() -> None:
    path = r"C:\Users\me\AppData\Roaming\herdr\sessions\baton\herdr.sock"
    # As seen on Windows 11 with herdr 0.9.5.
    assert pipe_name(Path(path)) == r"\\.\pipe" + "\\" + path


@pytest.mark.parametrize(
    "word", ["plain", "--model", "it's", "two words", '{"a":1}', "C:\\Program Files\\x", ""]
)
def test_a_quoted_word_reads_back_as_one_word_in_a_posix_shell(word: str) -> None:
    assert shlex.split(shell_quote(word, "linux")) == [word]


@pytest.mark.parametrize(
    ("word", "quoted"),
    [
        ("plain", "plain"),
        (r"C:\Users\me\x.json", r"C:\Users\me\x.json"),
        ("two words", "'two words'"),
        ("it's", "'it''s'"),
        (r"C:\Program Files\x", r"'C:\Program Files\x'"),
    ],
)
def test_powershell_gets_its_own_quoting(word: str, quoted: str) -> None:
    assert shell_quote(word, "windows") == quoted


def test_words_are_joined_with_their_quoting() -> None:
    assert shell_join(["--model", "a b"], "linux") == "--model 'a b'"
    assert shell_join(["--model", "a b"], "windows") == "--model 'a b'"


def test_the_lock_is_held_by_one_holder_at_a_time(tmp_path: Path) -> None:
    lock = tmp_path / "db.lock"
    order: list[str] = []
    entered = threading.Event()
    release = threading.Event()

    def first() -> None:
        with lock.open("a+", encoding="utf-8") as file, exclusive_lock(file):
            order.append("first in")
            entered.set()
            release.wait(5)
            order.append("first out")

    def second() -> None:
        entered.wait(5)
        with lock.open("a+", encoding="utf-8") as file, exclusive_lock(file, poll_s=0.01):
            order.append("second in")

    threads = [threading.Thread(target=first), threading.Thread(target=second)]
    for thread in threads:
        thread.start()
    entered.wait(5)
    release.set()
    for thread in threads:
        thread.join(5)
    assert order == ["first in", "first out", "second in"]
