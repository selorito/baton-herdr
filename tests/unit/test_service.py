from __future__ import annotations

import plistlib
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from baton_herdr.core.config import BatonSettings, HerdrSettings
from baton_herdr.core.fakes import FakeProcessTable
from baton_herdr.core.processes import ProcessEntry
from baton_herdr.service import (
    BATON_UNIT,
    HERDR_UNIT,
    ServiceError,
    ServiceProcesses,
    UnitPlan,
    install_launchd,
    install_start_script,
    install_units,
    install_windows_start_script,
    installed_session,
    launchd_session,
    render_launchd,
    render_start_script,
    render_units,
    render_windows_start_script,
    scheduled_task_command,
    search_path,
    service_kind,
    service_processes,
    service_session,
    status_lines,
    uninstall,
    windows_session,
    windows_task_command,
)

from platform_marks import posix_only

if TYPE_CHECKING:
    from collections.abc import Sequence

PLAN = UnitPlan(
    herdr="/home/u/.local/bin/herdr",
    baton="/home/u/.local/bin/baton",
    session="baton",
    path="/home/u/.local/bin:/usr/bin",
    lang="C.UTF-8",
    config=None,
)


@posix_only("systemd units and the WSL1 bash script")
def test_units_run_herdr_in_its_own_session_and_baton_against_it() -> None:
    units = render_units(PLAN)
    herdr, baton = units[HERDR_UNIT], units[BATON_UNIT]
    assert "ExecStart=/home/u/.local/bin/herdr --session baton server\n" in herdr
    assert f"Requires={HERDR_UNIT}\nAfter={HERDR_UNIT}\n" in baton
    assert "ExecStart=/home/u/.local/bin/baton daemon\n" in baton
    assert 'Environment="BATON_HERDR__SESSION=baton"\n' in baton
    assert 'Environment="PATH=/home/u/.local/bin:/usr/bin"\n' in herdr
    assert "BATON_CONFIG" not in baton  # the user default needs no pin
    pinned = render_units(replace(PLAN, config=Path("/srv/baton.toml")))
    assert 'Environment="BATON_CONFIG=/srv/baton.toml"\n' in pinned[BATON_UNIT]


@posix_only("systemd units and the WSL1 bash script")
def test_path_puts_the_found_binaries_first_without_duplicates() -> None:
    assert search_path(["/a/bin/herdr", None, "/a/bin/claude", "/b/baton"]) == (
        "/a/bin:/b:/usr/local/bin:/usr/bin:/bin"
    )


def test_install_writes_both_units_and_never_overwrites_silently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    written = install_units(PLAN, force=False)
    assert sorted(p.name for p in written) == sorted([BATON_UNIT, HERDR_UNIT])
    assert all(p.parent == tmp_path / "systemd" / "user" for p in written)

    with pytest.raises(ServiceError, match="--force"):
        install_units(PLAN, force=False)
    assert install_units(PLAN, force=True) == written


def test_baton_s_herdr_does_not_resume_agents_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    config = tmp_path / "baton" / "herdr-server.toml"
    plan = replace(PLAN, herdr_config=config)
    herdr = render_units(plan)[HERDR_UNIT]
    assert f'Environment="HERDR_CONFIG_PATH={config}"\n' in herdr
    assert "HERDR_CONFIG_PATH" not in render_units(plan)[BATON_UNIT]

    written = install_units(plan, force=False)
    assert config in written
    assert "resume_agents_on_restore = false" in config.read_text()


def test_unit_names_follow_adr_0010() -> None:
    assert sorted(render_units(PLAN)) == ["baton-herdr.service", "baton.service"]


def test_the_service_runs_the_configured_session_and_nothing_else() -> None:
    assert service_session(BatonSettings()) == "baton"
    assert service_session(BatonSettings(herdr=HerdrSettings(session="work"))) == "work"
    with pytest.raises(ServiceError, match="socket_path"):
        service_session(BatonSettings(herdr=HerdrSettings(socket_path=Path("/run/h.sock"))))
    for unnamed in (None, "default"):
        with pytest.raises(ServiceError, match="session of its own"):
            service_session(BatonSettings(herdr=HerdrSettings(session=unnamed)))


def test_the_installed_units_session_is_read_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))  # no start script either
    assert installed_session() is None
    install_units(replace(PLAN, session="work"), force=False)
    assert installed_session() == "work"


# WSL1 (ADR 0022, amended): one start script for a Windows scheduled task, no systemd.


@posix_only("systemd units and the WSL1 bash script")
def test_the_start_script_does_what_both_units_do(tmp_path: Path) -> None:
    config = tmp_path / "herdr-server.toml"
    script = render_start_script(replace(PLAN, herdr_config=config))
    assert script.startswith("#!/bin/bash\n")
    assert "/home/u/.local/bin/herdr --session baton server" in script
    assert f"HERDR_CONFIG_PATH={config}" in script
    assert "BATON_HERDR__SESSION=baton /home/u/.local/bin/baton daemon" in script
    assert "export PATH=/home/u/.local/bin:/usr/bin\n" in script
    assert "BATON_CONFIG" not in script  # the user default needs no pin
    assert "export HOME=${HOME:-/home/u}\n" in render_start_script(
        replace(PLAN, home=Path("/home/u"))
    )
    # Each restarts after it exits, as Restart= does, with the units' delays.
    assert script.count("while true; do") == 2
    assert "sleep 5\n" in script
    assert "sleep 10\n" in script
    pinned = render_start_script(replace(PLAN, config=Path("/srv/baton.toml")))
    assert "BATON_CONFIG=/srv/baton.toml BATON_HERDR__SESSION=baton" in pinned


@posix_only("systemd units and the WSL1 bash script")
def test_the_start_script_is_valid_bash(tmp_path: Path) -> None:
    path = tmp_path / "start"
    path.write_text(render_start_script(replace(PLAN, path="/a b/bin:/usr/bin")))
    subprocess.run(["bash", "-n", str(path)], check=True)  # noqa: S603, S607 - fixed


def test_a_second_start_while_one_runs_exits_and_leaves_it_alone() -> None:
    script = render_start_script(PLAN)
    # Only this install's herdr and batond count: their full paths, regex-escaped.
    assert r"pgrep -f -- '/home/u/\.local/bin/herdr --session baton server'" in script
    assert r"pgrep -f -- '/home/u/\.local/bin/baton daemon'" in script
    assert "exit 0" in script  # success, so the task does not restart it


@posix_only("systemd units and the WSL1 bash script")
def test_install_writes_the_script_executable_and_never_overwrites_silently(
    tmp_path: Path,
) -> None:
    script = tmp_path / "bin" / "baton-wsl1-start"
    config = tmp_path / "baton" / "herdr-server.toml"
    plan = replace(PLAN, herdr_config=config)
    assert install_start_script(plan, force=False, path=script) == [config, script]
    assert script.stat().st_mode & 0o777 == 0o755
    assert "resume_agents_on_restore = false" in config.read_text()

    with pytest.raises(ServiceError, match="--force"):
        install_start_script(plan, force=False, path=script)
    # A running bash reads its script as it goes: the file is replaced, never rewritten.
    before = script.stat().st_ino
    with script.open() as running:
        install_start_script(replace(plan, session="work"), force=True, path=script)
        assert "--session baton server" in running.read()
    assert script.stat().st_ino != before
    assert "--session work server" in script.read_text()


@posix_only("systemd units and the WSL1 bash script")
def test_the_scheduled_task_command_runs_the_script_under_wsl_exe() -> None:
    script = Path("/home/u/.local/bin/baton-wsl1-start")
    command = scheduled_task_command(script, "Ubuntu-24.04")
    assert (
        "New-ScheduledTaskAction -Execute 'wsl.exe' "
        "-Argument '-d Ubuntu-24.04 -- /home/u/.local/bin/baton-wsl1-start'"
    ) in command
    assert "-AtStartup" in command
    assert "-AtLogOn" in command
    assert "-LogonType S4U" in command  # runs whether or not you are signed in
    assert "-ExecutionTimeLimit ([TimeSpan]::Zero)" in command
    assert "-RestartInterval (New-TimeSpan -Minutes 1)" in command
    assert "Register-ScheduledTask -TaskName 'baton'" in command
    assert "Start-ScheduledTask -TaskName 'baton'" in command
    assert "'-d <distro> --" in scheduled_task_command(script, None)
    quoted = scheduled_task_command(Path("/home/o'neil/start"), "Ubuntu")
    assert "-Argument '-d Ubuntu -- /home/o''neil/start'" in quoted


def test_the_installed_scripts_session_is_read_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    script = tmp_path / "baton-wsl1-start"
    assert installed_session(script=script) is None
    install_start_script(replace(PLAN, session="work"), force=False, path=script)
    assert installed_session(script=script) == "work"


def test_the_services_processes_are_found_by_their_arguments() -> None:
    table = FakeProcessTable(
        [
            ProcessEntry(10, 1, "herdr", "S"),
            ProcessEntry(11, 1, "herdr", "S"),
            ProcessEntry(12, 1, "python", "S"),
            ProcessEntry(13, 1, "herdr", "S"),
            ProcessEntry(14, 1, "baton", "Z"),
        ],
        argv={
            10: ["/bin/herdr", "--session", "baton", "server"],
            11: ["/bin/herdr", "--session", "work", "server"],
            12: ["/venv/python", "/bin/baton", "daemon"],
            13: ["/bin/herdr", "--session", "baton"],  # a client, not the server
            14: ["/bin/baton", "daemon"],  # exiting
        },
    )
    assert service_processes(table, "baton") == ServiceProcesses(herdr=[10], batond=[12])


def test_status_says_what_is_installed_and_what_runs(tmp_path: Path) -> None:
    script = tmp_path / "baton-wsl1-start"
    running = ServiceProcesses(herdr=[10], batond=[12])
    lines = status_lines(wsl1=True, installed=None, files=[script], processes=running)
    assert lines[0] == f"start script: not installed ({script}); run baton service install"
    assert "herdr server: running (pid 10)" in lines
    assert "batond: running (pid 12)" in lines
    assert any("Get-ScheduledTask -TaskName 'baton'" in line for line in lines)

    stopped = ServiceProcesses(herdr=[], batond=[])
    units = [tmp_path / HERDR_UNIT, tmp_path / BATON_UNIT]
    lines = status_lines(wsl1=False, installed="baton", files=units, processes=stopped)
    assert lines[0] == f"units: {units[0]}, {units[1]} (herdr session 'baton')"
    assert "batond: not running" in lines
    assert any("systemctl --user status" in line for line in lines)


def test_uninstall_on_wsl1_removes_the_script_and_says_how_to_remove_the_task(
    tmp_path: Path,
) -> None:
    script = tmp_path / "baton-wsl1-start"
    install_start_script(PLAN, force=False, path=script)
    ran: list[Sequence[str]] = []

    def run(command: Sequence[str]) -> tuple[int, str]:
        ran.append(command)
        return 0, ""

    lines = uninstall(wsl1=True, run=run, script=script)
    assert not script.exists()
    assert ran == []  # never a Windows command
    assert f"removed {script}" in lines
    assert any("Unregister-ScheduledTask -TaskName 'baton'" in line for line in lines)
    assert uninstall(wsl1=True, run=lambda c: (0, ""), script=script)[0].startswith(
        "nothing to remove"
    )


def test_uninstall_with_systemd_stops_the_units_before_removing_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    written = install_units(PLAN, force=False)
    ran: list[list[str]] = []

    def run(command: Sequence[str]) -> tuple[int, str]:
        ran.append(list(command))
        assert all(path.exists() for path in written) or command[-1] == "daemon-reload"
        return 0, ""

    lines = uninstall(wsl1=False, run=run)
    assert ran == [
        ["systemctl", "--user", "disable", "--now", BATON_UNIT, HERDR_UNIT],
        ["systemctl", "--user", "daemon-reload"],
    ]
    assert not any(path.exists() for path in written)
    assert sorted(lines) == sorted(f"removed {path}" for path in written)


@pytest.mark.parametrize(
    ("system", "wsl1", "kind"),
    [
        ("linux", False, "systemd"),
        ("linux", True, "wsl1"),
        ("macos", False, "launchd"),
        ("windows", False, "windows"),
    ],
)
def test_each_system_gets_its_own_service(system: str, wsl1: bool, kind: str) -> None:
    assert service_kind(system, wsl1=wsl1) == kind  # type: ignore[arg-type]


WINDOWS_PLAN = UnitPlan(
    herdr=r"C:\Users\u\.local\bin\herdr.exe",
    baton=r"C:\Users\u\.local\bin\baton.exe",
    session="baton",
    path=r"C:\Users\u\.local\bin;C:\Windows\system32",
    lang="C.UTF-8",
    config=Path(r"C:\Users\u\it's\baton.toml"),
    herdr_config=Path(r"C:\Users\u\.config\baton\herdr-server.toml"),
)


def test_the_windows_start_script_does_what_both_units_do(tmp_path: Path) -> None:
    script = render_windows_start_script(WINDOWS_PLAN)
    assert r"$env:PATH = 'C:\Users\u\.local\bin;C:\Windows\system32'" in script
    assert "$env:BATON_HERDR__SESSION = 'baton'" in script
    assert r"$env:BATON_CONFIG = 'C:\Users\u\it''s\baton.toml'" in script  # quote doubled
    assert r"$env:HERDR_CONFIG_PATH = 'C:\Users\u\.config\baton\herdr-server.toml'" in script
    assert "$env:PYTHONUTF8 = '1'" in script
    assert r"Start-Logged 'C:\Users\u\.local\bin\herdr.exe' @('--session', 'baton', 'server')" in (
        script
    )
    assert r"Start-Logged 'C:\Users\u\.local\bin\baton.exe' @('daemon') 'daemon'" in script
    assert "already running" in script  # a second copy leaves the first alone
    path = tmp_path / "baton-start.ps1"
    install_windows_start_script(replace(WINDOWS_PLAN, herdr_config=None), force=False, path=path)
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")  # PowerShell 5.1 reads it as UTF-8
    assert windows_session(path) == "baton"
    with pytest.raises(ServiceError, match="already exists"):
        install_windows_start_script(WINDOWS_PLAN, force=False, path=path)


def test_the_windows_task_runs_the_script_hidden_and_restarts_it() -> None:
    command = windows_task_command(Path(r"C:\Users\u\.local\bin\baton-start.ps1"))
    assert "-Execute 'powershell.exe'" in command
    assert r'-File "C:\Users\u\.local\bin\baton-start.ps1"' in command
    assert "-LogonType S4U" in command
    assert "-RestartCount 9999" in command
    assert "Start-ScheduledTask -TaskName 'baton'" in command


def test_the_windows_path_keeps_the_found_binaries_first(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", r"C:\Windows\system32;C:\Tools")
    found = search_path([r"C:\Tools\herdr.exe", r"C:\bin\baton.exe"], "windows")
    assert found == r"C:\Tools;C:\bin;C:\Windows\system32"


def test_macos_path_has_homebrew() -> None:
    assert search_path(["/Users/u/.local/bin/herdr"], "macos") == (
        "/Users/u/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
    )


def test_launch_agents_keep_herdr_and_batond_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    plan = replace(PLAN, herdr_config=tmp_path / "herdr-server.toml", config=tmp_path / "b.toml")
    agents = {name: plistlib.loads(data) for name, data in render_launchd(plan).items()}
    herdr = agents["dev.baton-herdr.herdr.plist"]
    batond = agents["dev.baton-herdr.batond.plist"]
    assert herdr["ProgramArguments"] == [PLAN.herdr, "--session", "baton", "server"]
    assert herdr["EnvironmentVariables"]["HERDR_CONFIG_PATH"] == str(tmp_path / "herdr-server.toml")
    assert batond["ProgramArguments"] == [PLAN.baton, "daemon"]
    assert batond["EnvironmentVariables"]["BATON_HERDR__SESSION"] == "baton"
    assert batond["EnvironmentVariables"]["BATON_CONFIG"] == str(tmp_path / "b.toml")
    assert all(a["KeepAlive"] and a["RunAtLoad"] for a in agents.values())
    written = install_launchd(plan, force=False, directory=tmp_path / "LaunchAgents")
    assert launchd_session(tmp_path / "LaunchAgents") == "baton"
    assert len(written) == 3  # herdr's settings and both agents
    with pytest.raises(ServiceError, match="already exists"):
        install_launchd(plan, force=False, directory=tmp_path / "LaunchAgents")
