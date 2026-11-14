"""``baton service install``: systemd user units for batond and its herdr server.

Two units:

- ``baton-herdr.service`` runs a herdr server in a session of baton's own. Started
  by systemd, it gets a clean environment: a herdr server started from a shell
  inside an agent inherits that agent's variables, and the agents it then starts
  misbehave (Claude Code wrote no transcript, so its sessions could not be
  resumed; docs/research/agents.md).
- ``baton.service`` runs ``baton daemon`` against that session.

The session is ``[herdr] session`` from the settings, the same value the CLI and
``baton doctor`` use, so they all reach the same herdr server.

PATH is built from the directories of the binaries found now (herdr, baton and
the configured agents), so the panes find the same agents as your shell does.
The units are only written; enabling them is left to the user.

WSL1 has no systemd (ADR 0022, amended). There ``baton service install`` writes one start
script that does what both units do, and prints the PowerShell command that registers a
Windows scheduled task to run it under ``wsl.exe``: WSL1 ends every process of the
distribution when the last ``wsl.exe`` attached to it exits, so the script stays in the
foreground for as long as baton should run. baton never runs a Windows command itself.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from baton_herdr.adapters import ADAPTERS
from baton_herdr.core.config import HERDR_DEFAULT_SESSION_NAME, user_config_dir
from baton_herdr.core.executables import detector_binary
from baton_herdr.core.model import AgentKind

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    from baton_herdr.core.config import BatonSettings
    from baton_herdr.core.processes import ProcessTable

HERDR_UNIT = "baton-herdr.service"
# herdr's settings for the server baton's agents run in, beside baton's own config. Only
# what baton needs differs from herdr's defaults; the user's own herdr is not touched.
HERDR_SERVER_CONFIG = """\
# herdr settings for the server baton runs its agents in (written by baton service install).
# After a restart baton resumes the agents of its tasks itself. herdr resuming every pane's
# agent as well would start a second, idle copy of each, holding the session baton resumes.
[session]
resume_agents_on_restore = false
"""
BATON_UNIT = "baton.service"
# WSL1: the script a Windows scheduled task runs, and that task's name.
START_SCRIPT = "baton-wsl1-start"
TASK_NAME = "baton"
_SESSION_ARGUMENT = re.compile(r"--session\s+'?([^\s']+)'?\s+server")
_SYSTEM_PATH = ("/usr/local/bin", "/usr/bin", "/bin")


@dataclass(frozen=True, slots=True)
class UnitPlan:
    herdr: str
    baton: str
    session: str
    path: str
    lang: str
    config: Path | None  # pinned with BATON_CONFIG when the file is not the user default
    # herdr's settings for this server (HERDR_SERVER_CONFIG), passed as HERDR_CONFIG_PATH.
    herdr_config: Path | None = None
    # WSL1's start script: HOME when the task's wsl.exe gives none.
    home: Path | None = None


def render_units(plan: UnitPlan) -> dict[str, str]:
    """The unit files' contents, by file name."""
    herdr = shlex.quote(plan.herdr)
    session = shlex.quote(plan.session)
    shared_env = f'Environment="PATH={plan.path}"\nEnvironment="LANG={plan.lang}"\n'
    config_env = f'Environment="BATON_CONFIG={plan.config}"\n' if plan.config else ""
    herdr_env = (
        f'Environment="HERDR_CONFIG_PATH={plan.herdr_config}"\n' if plan.herdr_config else ""
    )
    return {
        HERDR_UNIT: (
            "[Unit]\n"
            f"Description=herdr server for baton (session {plan.session})\n"
            "\n"
            "[Service]\n"
            f"ExecStart={herdr} --session {session} server\n"
            f"ExecStop={herdr} --session {session} server stop\n"
            f"{shared_env}"
            f"{herdr_env}"
            "Environment=TERM=xterm-256color\n"
            "Restart=on-failure\n"
            "RestartSec=5\n"
            "\n"
            "[Install]\n"
            "WantedBy=default.target\n"
        ),
        BATON_UNIT: (
            "[Unit]\n"
            "Description=baton: keeps coding agents working on their tasks (batond)\n"
            f"Requires={HERDR_UNIT}\n"
            f"After={HERDR_UNIT}\n"
            "\n"
            "[Service]\n"
            f"ExecStart={shlex.quote(plan.baton)} daemon\n"
            f"{shared_env}"
            f'Environment="BATON_HERDR__SESSION={plan.session}"\n'
            f"{config_env}"
            # The herdr socket may take a moment to appear after its server starts.
            "Restart=on-failure\n"
            "RestartSec=10\n"
            "\n"
            "[Install]\n"
            "WantedBy=default.target\n"
        ),
    }


def unit_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    return (Path(base) if base else Path.home() / ".config") / "systemd" / "user"


def search_path(binaries: Iterable[str | None]) -> str:
    """PATH with the directories of ``binaries`` first, then the system's."""
    dirs: list[str] = []
    for binary in binaries:
        if binary:
            parent = str(Path(binary).parent)
            if parent not in dirs:
                dirs.append(parent)
    dirs.extend(d for d in _SYSTEM_PATH if d not in dirs)
    return ":".join(dirs)


class ServiceError(Exception):
    """The units cannot be written; the message says why."""


def service_session(settings: BatonSettings) -> str:
    """The herdr session the service runs: the configured one, which must be named."""
    if settings.herdr.socket_path is not None:
        msg = (
            "[herdr] socket_path is set, so baton would not use the service's herdr server. "
            'Remove socket_path and set [herdr] session (default "baton").'
        )
        raise ServiceError(msg)
    session = settings.herdr.session
    if not session or session == HERDR_DEFAULT_SESSION_NAME:
        msg = (
            "The service runs herdr in a session of its own; set [herdr] session to a name "
            '(default "baton") instead of herdr\'s default session.'
        )
        raise ServiceError(msg)
    return session


def installed_session(directory: Path | None = None, script: Path | None = None) -> str | None:
    """The herdr session an installed baton-herdr.service, or else WSL1's start script,
    runs; ``None`` when neither is installed."""
    try:
        return session_in_unit((directory or unit_dir()).joinpath(HERDR_UNIT).read_text("utf-8"))
    except OSError:
        pass
    try:
        text = (script or start_script_path()).read_text(encoding="utf-8")
    except OSError:
        return None
    match = _SESSION_ARGUMENT.search(text)
    return match.group(1) if match else None


def session_in_unit(text: str) -> str | None:
    for line in text.splitlines():
        if line.startswith("ExecStart=") and (match := _SESSION_ARGUMENT.search(line)):
            return match.group(1)
    return None


def plan_units(settings: BatonSettings, *, config: Path) -> UnitPlan:
    session = service_session(settings)
    herdr = shutil.which("herdr")
    if herdr is None:
        msg = "herdr is not on PATH; install it first."
        raise ServiceError(msg)
    baton = shutil.which("baton") or str(Path(sys.argv[0]).resolve())
    agents = []
    for name in settings.scheduler.agents:
        command = (
            settings.scheduler.launch_commands.get(name)
            or ADAPTERS[AgentKind(name)].launch_command()
        )
        agents.append(shutil.which(shlex.split(command)[0]))
    detector = detector_binary(settings.usage.binary) if settings.usage.enabled else None
    default_config = Path.home() / ".config" / "baton" / "baton.toml"
    pinned = config.resolve() if config.is_file() and config.resolve() != default_config else None
    return UnitPlan(
        herdr=herdr,
        baton=baton,
        session=session,
        path=search_path([herdr, baton, str(detector) if detector else None, *agents]),
        lang=os.environ.get("LANG") or "C.UTF-8",
        config=pinned,
        herdr_config=user_config_dir() / "herdr-server.toml",
        home=Path.home(),
    )


def install_units(plan: UnitPlan, *, force: bool) -> list[Path]:
    directory = unit_dir()
    files = render_units(plan)
    _refuse_existing([directory / name for name in files], force=force)
    directory.mkdir(parents=True, exist_ok=True)
    written = _write_herdr_config(plan)
    for name, text in files.items():
        path = directory / name
        path.write_text(text, encoding="utf-8")
        written.append(path)
    return written


def _refuse_existing(paths: Iterable[Path], *, force: bool) -> None:
    existing = [path for path in paths if path.exists()]
    if existing and not force:
        names = ", ".join(str(path) for path in existing)
        msg = f"{names} already exists; use --force to replace."
        raise ServiceError(msg)


def _write_herdr_config(plan: UnitPlan) -> list[Path]:
    if plan.herdr_config is None:
        return []
    # baton's own file: always brought up to date.
    plan.herdr_config.parent.mkdir(parents=True, exist_ok=True)
    plan.herdr_config.write_text(HERDR_SERVER_CONFIG, encoding="utf-8")
    return [plan.herdr_config]


def start_script_path() -> Path:
    return Path.home() / ".local" / "bin" / START_SCRIPT


def _pgrep_pattern(text: str) -> str:
    """``text`` as an extended regular expression that matches only itself."""
    return shlex.quote(re.sub(r"([.^$*+?()\[\]{}|\\])", r"\\\1", text))


def render_start_script(plan: UnitPlan) -> str:
    """The script that runs on WSL1 what the two units run elsewhere (ADR 0022).

    It stays in the foreground: WSL1 ends every process of the distribution when the
    ``wsl.exe`` it runs under exits. A second copy started while one runs (the task fires
    at boot and at logon) exits at once, with success so that the task does not retry it.
    """
    herdr, baton = shlex.quote(plan.herdr), shlex.quote(plan.baton)
    session = shlex.quote(plan.session)
    server = f"{plan.herdr} --session {plan.session} server"
    daemon = f"{plan.baton} daemon"
    herdr_env = (
        f"HERDR_CONFIG_PATH={shlex.quote(str(plan.herdr_config))} " if plan.herdr_config else ""
    )
    config_env = f"BATON_CONFIG={shlex.quote(str(plan.config))} " if plan.config else ""
    home = f"export HOME=${{HOME:-{shlex.quote(str(plan.home))}}}\n" if plan.home else ""
    return f"""\
#!/bin/bash
# Starts herdr and batond on WSL1, where there is no systemd (ADR 0022).
# Written by baton service install; run by the Windows scheduled task {TASK_NAME!r} as
# wsl.exe -d <distro> -- <this script>. WSL1 ends every process of the distribution when
# the last wsl.exe attached to it exits, so this script stays in the foreground.
# Mirrors {HERDR_UNIT} and {BATON_UNIT}: each restarts after it exits.
set -u
{home}export PATH={shlex.quote(plan.path)}
export LANG={shlex.quote(plan.lang)} TERM=xterm-256color
LOG="${{XDG_STATE_HOME:-$HOME/.local/state}}/baton"
mkdir -p "$LOG"

if pgrep -f -- {_pgrep_pattern(server)} >/dev/null \\
    || pgrep -f -- {_pgrep_pattern(daemon)} >/dev/null; then
    echo "$(date -Is) already running" >>"$LOG/wsl1-start.log"
    exit 0
fi
echo "$(date -Is) starting" >>"$LOG/wsl1-start.log"

(
    while true; do
        {herdr_env}{herdr} --session {session} server >>"$LOG/herdr-server.log" 2>&1
        echo "$(date -Is) herdr server exited: $?" >>"$LOG/wsl1-start.log"
        sleep 5
    done
) &

# The herdr socket may take a moment to appear after its server starts.
sleep 5
while true; do
    {config_env}BATON_HERDR__SESSION={session} {baton} daemon >>"$LOG/daemon.log" 2>&1
    echo "$(date -Is) batond exited: $?" >>"$LOG/wsl1-start.log"
    sleep 10
done
"""


def install_start_script(plan: UnitPlan, *, force: bool, path: Path | None = None) -> list[Path]:
    script = path or start_script_path()
    _refuse_existing([script], force=force)
    written = _write_herdr_config(plan)
    script.parent.mkdir(parents=True, exist_ok=True)
    # Replaced, never rewritten in place: a running bash reads its script as it goes.
    fd, temporary = tempfile.mkstemp(dir=script.parent, prefix=f".{script.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(render_start_script(plan))
        Path(temporary).chmod(0o755)
        Path(temporary).replace(script)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    written.append(script)
    return written


def _powershell_quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def scheduled_task_command(script: Path, distro: str | None) -> str:
    """PowerShell, run as administrator, that registers and starts the task running
    ``script`` on WSL1: at boot and at logon, signed in or not (S4U), without a time limit,
    restarted every minute after a failure."""
    arguments = f"-d {distro or '<distro>'} -- {script}"
    task = _powershell_quote(TASK_NAME)
    return (
        f"$action = New-ScheduledTaskAction -Execute 'wsl.exe' "
        f"-Argument {_powershell_quote(arguments)}\n"
        "$triggers = @((New-ScheduledTaskTrigger -AtStartup), "
        "(New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME))\n"
        '$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\\$env:USERNAME" '
        "-LogonType S4U\n"
        "$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) "
        "-RestartCount 9999 -RestartInterval (New-TimeSpan -Minutes 1) "
        "-MultipleInstances IgnoreNew -StartWhenAvailable "
        "-AllowStartIfOnBatteries -DontStopIfGoingOnBatteries\n"
        f"Register-ScheduledTask -TaskName {task} -Action $action -Trigger $triggers "
        "-Principal $principal -Settings $settings -Force\n"
        f"Start-ScheduledTask -TaskName {task}\n"
    )


WSL1_NEXT_STEPS = """\
WSL1 has no systemd: a Windows scheduled task runs the script under wsl.exe.
Next, in PowerShell as administrator (on Windows, not in WSL):

{command}
Then:
  baton service status                         # is herdr's server and batond running?
  herdr --session {session}                    # watch the agents work
Logs: ~/.local/state/baton/ (daemon.log, herdr-server.log, wsl1-start.log)
"""


NEXT_STEPS = """\
Next:
  systemctl --user daemon-reload
  systemctl --user enable --now {baton_unit}   # starts baton-herdr.service too
  loginctl enable-linger "$USER"               # keep running after you log out
  journalctl --user -u {baton_unit} -f         # follow batond's log
  herdr --session {session}                    # watch the agents work
"""


@dataclass(frozen=True, slots=True)
class ServiceProcesses:
    herdr: list[int]  # herdr servers of the service's session
    batond: list[int]


def service_processes(table: ProcessTable, session: str) -> ServiceProcesses:
    """The service's herdr server and batond, found by their arguments."""
    server = ["--session", session, "server"]
    herdr, batond = [], []
    for entry in table.entries():
        if entry.state in {"Z", "X"}:
            continue  # exiting; reading its cmdline can block on WSL
        argv = list(table.argv(entry.pid) or [])
        if argv and Path(argv[0]).name == "herdr" and argv[1:] == server:
            herdr.append(entry.pid)
        elif any(
            Path(arg).name == "baton" and rest[:1] == ["daemon"]
            for arg, rest in ((argv[i], argv[i + 1 :]) for i in range(len(argv)))
        ):
            batond.append(entry.pid)
    return ServiceProcesses(herdr=herdr, batond=batond)


def _running(pids: Sequence[int]) -> str:
    if not pids:
        return "not running"
    return f"running (pid {', '.join(str(pid) for pid in pids)})"


def status_lines(
    *, wsl1: bool, installed: str | None, files: Sequence[Path], processes: ServiceProcesses
) -> list[str]:
    """``baton service status``: what is installed, what runs, and where to look further."""
    what = "start script" if wsl1 else "units"
    where = ", ".join(str(path) for path in files)
    lines = [
        f"{what}: {where} (herdr session {installed!r})"
        if installed is not None
        else f"{what}: not installed ({where}); run baton service install",
        f"herdr server: {_running(processes.herdr)}",
        f"batond: {_running(processes.batond)}",
    ]
    if wsl1:
        lines.append(
            "Windows task: in PowerShell, "
            f"Get-ScheduledTask -TaskName {_powershell_quote(TASK_NAME)} | Get-ScheduledTaskInfo"
        )
    else:
        lines.append(f"systemd: systemctl --user status {BATON_UNIT} {HERDR_UNIT}")
    return lines


def uninstall(
    *,
    wsl1: bool,
    run: Callable[[Sequence[str]], tuple[int, str]],
    script: Path | None = None,
    directory: Path | None = None,
) -> list[str]:
    """Remove what ``baton service install`` wrote; herdr's settings for the server stay.

    With systemd, the units are disabled and stopped first. On WSL1 nothing is run: the
    lines say how to remove the Windows task, which only Windows can do.
    """
    if wsl1:
        path = script or start_script_path()
        if not path.exists():
            return [f"nothing to remove: {path} does not exist"]
        path.unlink()
        task = _powershell_quote(TASK_NAME)
        return [
            f"removed {path}",
            "The Windows task still exists; remove it in PowerShell as administrator. "
            "herdr and batond stop when its wsl.exe ends:",
            f"  Stop-ScheduledTask -TaskName {task}; "
            f"Unregister-ScheduledTask -TaskName {task} -Confirm:$false",
        ]
    units = [(directory or unit_dir()) / name for name in (BATON_UNIT, HERDR_UNIT)]
    present = [path for path in units if path.exists()]
    if not present:
        return [f"nothing to remove: no {BATON_UNIT} or {HERDR_UNIT} in {units[0].parent}"]
    lines = []
    code, output = run(["systemctl", "--user", "disable", "--now", BATON_UNIT, HERDR_UNIT])
    if code != 0:
        lines.append(f"systemctl --user disable --now failed: {output.strip()}")
    for path in present:
        path.unlink()
        lines.append(f"removed {path}")
    code, output = run(["systemctl", "--user", "daemon-reload"])
    if code != 0:
        lines.append(f"systemctl --user daemon-reload failed: {output.strip()}")
    return lines
