"""``baton service install``: systemd user units for batond and its herdr server.

Two units:

- ``baton-herdr.service`` runs a herdr server in a session of baton's own. Started
  by systemd, it gets a clean environment: a herdr server started from a shell
  inside an agent inherits that agent's variables, and the agents it then starts
  misbehave (Claude Code wrote no transcript, so its sessions could not be
  resumed; docs/research/agents.md).
- ``baton_herdr.service`` runs ``baton daemon`` against that session.

PATH is built from the directories of the binaries found now (herdr, baton and
the configured agents), so the panes find the same agents as your shell does.
The units are only written; enabling them is left to the user.
"""

from __future__ import annotations

import os
import shlex
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from baton_herdr.adapters import ADAPTERS
from baton_herdr.core.model import AgentKind

if TYPE_CHECKING:
    from collections.abc import Iterable

    from baton_herdr.core.config import BatonSettings

HERDR_UNIT = "baton-herdr.service"
BATON_UNIT = "baton_herdr.service"
DEFAULT_SESSION = "baton"
_SYSTEM_PATH = ("/usr/local/bin", "/usr/bin", "/bin")


@dataclass(frozen=True, slots=True)
class UnitPlan:
    herdr: str
    baton: str
    session: str
    path: str
    lang: str
    config: Path | None  # pinned with BATON_CONFIG when the file is not the user default


def render_units(plan: UnitPlan) -> dict[str, str]:
    """The unit files' contents, by file name."""
    herdr = shlex.quote(plan.herdr)
    session = shlex.quote(plan.session)
    shared_env = f'Environment="PATH={plan.path}"\nEnvironment="LANG={plan.lang}"\n'
    config_env = f'Environment="BATON_CONFIG={plan.config}"\n' if plan.config else ""
    return {
        HERDR_UNIT: (
            "[Unit]\n"
            f"Description=herdr server for baton (session {plan.session})\n"
            "\n"
            "[Service]\n"
            f"ExecStart={herdr} --session {session} server\n"
            f"ExecStop={herdr} --session {session} server stop\n"
            f"{shared_env}"
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


def plan_units(settings: BatonSettings, *, session: str, config: Path) -> UnitPlan:
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
    default_config = Path.home() / ".config" / "baton" / "baton.toml"
    pinned = config.resolve() if config.is_file() and config.resolve() != default_config else None
    return UnitPlan(
        herdr=herdr,
        baton=baton,
        session=session,
        path=search_path([herdr, baton, *agents]),
        lang=os.environ.get("LANG") or "C.UTF-8",
        config=pinned,
    )


def install_units(plan: UnitPlan, *, force: bool) -> list[Path]:
    directory = unit_dir()
    files = render_units(plan)
    existing = [directory / name for name in files if (directory / name).exists()]
    if existing and not force:
        names = ", ".join(str(path) for path in existing)
        msg = f"{names} already exists; use --force to replace."
        raise ServiceError(msg)
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for name, text in files.items():
        path = directory / name
        path.write_text(text, encoding="utf-8")
        written.append(path)
    return written


NEXT_STEPS = """\
Next:
  systemctl --user daemon-reload
  systemctl --user enable --now {baton_unit}
  loginctl enable-linger "$USER"     # keep running after you log out
  journalctl --user -u {baton_unit} -f   # follow batond's log
  herdr --session {session}          # watch the agents work
"""
