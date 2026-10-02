"""``baton doctor``: check that everything baton needs is in place, and say how to fix it.

Each check reports ok, a warning (works, but probably not what you want) or a
failure (batond will not work). Side effects (looking up binaries, running
``herdr``, talking to the herdr server and to Telegram) are behind ``Probes``,
so the checks themselves are tested without them.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import ValidationError

from baton_herdr.adapters import ADAPTERS
from baton_herdr.core.config import BatonSettings, config_file, resolve_herdr_socket_path
from baton_herdr.core.model import AgentKind

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from baton_herdr.core.config import HerdrSettings, TelegramSettings


class Level(StrEnum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    level: Level
    detail: str
    hint: str = ""


async def _ping_herdr(settings: HerdrSettings) -> str:
    from baton_herdr.herdr import HerdrSocket  # noqa: PLC0415 - only when the check runs

    path = resolve_herdr_socket_path(settings, env=os.environ, home=Path.home())
    await HerdrSocket(path).request("workspace.list", {})
    return str(path)


async def _telegram_bot_name(settings: TelegramSettings) -> str:
    from aiogram import Bot  # noqa: PLC0415 - only when Telegram is configured

    if settings.bot_token is None:
        return ""
    bot = Bot(token=settings.bot_token.get_secret_value())
    try:
        me = await bot.get_me()
    finally:
        await bot.session.close()
    return me.username or str(me.id)


async def _count_events(path: Path) -> int:
    from baton_herdr.ledger import open_event_store  # noqa: PLC0415 - only when the check runs

    store = await open_event_store(path)
    try:
        return len(await store.read())
    finally:
        await store.close()


def _run(command: Sequence[str]) -> str:
    result = subprocess.run(  # noqa: S603 - fixed argument lists, no shell
        list(command), capture_output=True, text=True, timeout=15, check=True
    )
    return result.stdout


def _local_timezone() -> str | None:
    link = Path("/etc/localtime")
    if link.is_symlink():
        target = str(link.resolve())
        if "zoneinfo/" in target:
            return target.split("zoneinfo/", 1)[1]
    return os.environ.get("TZ")


@dataclass(frozen=True, slots=True)
class Probes:
    which: Callable[[str], str | None] = shutil.which
    run: Callable[[Sequence[str]], str] = _run
    ping_herdr: Callable[[HerdrSettings], Awaitable[str]] = _ping_herdr
    telegram_bot_name: Callable[[TelegramSettings], Awaitable[str]] = _telegram_bot_name
    count_events: Callable[[Path], Awaitable[int]] = _count_events
    local_timezone: Callable[[], str | None] = _local_timezone


async def diagnose(probes: Probes | None = None) -> list[Check]:
    """Run every check; later checks are skipped when the config cannot be read."""
    probes = probes or Probes()
    path = config_file()
    try:
        settings = BatonSettings()
    except ValidationError as err:
        first = err.errors()[0]
        where = ".".join(str(part) for part in first["loc"])
        return [
            Check(
                "config",
                Level.FAIL,
                f"{path}: {where}: {first['msg']}",
                "Fix the setting, or compare with baton.example.toml.",
            )
        ]
    checks = [
        Check("config", Level.OK, str(path))
        if path.is_file()
        else Check(
            "config",
            Level.WARN,
            f"no {path}; using defaults",
            f"Copy baton.example.toml to {path} and edit it.",
        )
    ]
    checks.append(await _database(settings, probes))
    checks.extend(await _herdr(settings, probes))
    checks.extend(_agents(settings, probes))
    checks.append(_timezone(settings, probes))
    checks.append(await _telegram(settings, probes))
    return checks


async def _database(settings: BatonSettings, probes: Probes) -> Check:
    path = settings.database.path
    if not path.exists():
        # Checking must not create it; say whether it can be created.
        parent = next((p for p in path.parents if p.exists()), None)
        if parent is None or not os.access(parent, os.W_OK):
            return Check(
                "database", Level.FAIL, f"{path} cannot be created", "Choose a writable path."
            )
        return Check("database", Level.OK, f"{path} (created on first use)")
    try:
        count = await probes.count_events(path)
    except Exception as err:  # noqa: BLE001 - any failure is the finding
        return Check(
            "database", Level.FAIL, f"{path}: {err}", "Check that the directory is writable."
        )
    return Check("database", Level.OK, f"{path} ({count} events)")


async def _herdr(settings: BatonSettings, probes: Probes) -> list[Check]:
    binary = probes.which("herdr")
    if binary is None:
        return [
            Check(
                "herdr",
                Level.FAIL,
                "herdr is not on PATH",
                "Install herdr (https://github.com/herdrdev/herdr), version 0.9.1 or later.",
            )
        ]
    try:
        version = probes.run([binary, "--version"]).strip()
    except (OSError, subprocess.SubprocessError) as err:
        version = f"version unknown ({err})"
    checks = [Check("herdr", Level.OK, f"{version} at {binary}")]

    session = settings.herdr.session
    try:
        socket = await probes.ping_herdr(settings.herdr)
        checks.append(Check("herdr server", Level.OK, f"reachable at {socket}"))
    except Exception as err:  # noqa: BLE001 - any failure is the finding
        start = f"herdr --session {session} server" if session else "herdr server"
        checks.append(
            Check(
                "herdr server",
                Level.FAIL,
                f"not reachable ({type(err).__name__})",
                f"Start it ({start}), or run `baton service install` to have systemd do it.",
            )
        )

    try:
        statuses = parse_integration_status(probes.run([binary, "integration", "status"]))
    except (OSError, subprocess.SubprocessError) as err:
        checks.append(Check("integrations", Level.WARN, f"could not read ({err})"))
        return checks
    for agent in settings.scheduler.agents:
        status = statuses.get(agent, "unknown")
        healthy = status.startswith("current")
        checks.append(
            Check(
                f"{agent} integration",
                Level.OK if healthy else Level.FAIL,
                status,
                ""
                if healthy
                else f"Run `herdr integration install {agent}`: without it herdr reports no "
                "session id, and baton cannot resume or verify this agent's sessions.",
            )
        )
    return checks


def parse_integration_status(text: str) -> dict[str, str]:
    """``herdr integration status`` lines, e.g. ``claude: current (v10) (/path)``."""
    statuses: dict[str, str] = {}
    for line in text.splitlines():
        name, sep, rest = line.partition(": ")
        if not sep:
            continue
        name = name.removesuffix(" (experimental)").strip()
        statuses[name] = rest.rsplit(" (/", 1)[0].strip()
    return statuses


def _agents(settings: BatonSettings, probes: Probes) -> list[Check]:
    checks = []
    for name in settings.scheduler.agents:
        agent = AgentKind(name)
        command = settings.scheduler.launch_commands.get(name) or ADAPTERS[agent].launch_command()
        program = shlex.split(command)[0]
        found = probes.which(program)
        checks.append(
            Check(name, Level.OK, f"{command!r} -> {found}")
            if found
            else Check(
                name,
                Level.FAIL,
                f"{program} is not on PATH",
                f"Install {name}, or remove it from [scheduler] agents.",
            )
        )
    return checks


def _timezone(settings: BatonSettings, probes: Probes) -> Check:
    zone = settings.scheduler.timezone
    try:
        ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError):
        return Check(
            "timezone", Level.FAIL, f"unknown zone {zone!r}", "Use an IANA name: Europe/Istanbul."
        )
    local = probes.local_timezone()
    if local and local != zone:
        return Check(
            "timezone",
            Level.WARN,
            f"{zone}, but this machine is on {local}",
            "Agents print reset times in local time; set [scheduler] timezone = "
            f'"{local}" so limits are read correctly.',
        )
    return Check("timezone", Level.OK, zone)


async def _telegram(settings: BatonSettings, probes: Probes) -> Check:
    telegram = settings.telegram
    if telegram.bot_token is None and telegram.chat_id is None:
        return Check(
            "telegram",
            Level.WARN,
            "not configured; notices go to the log only",
            "See the README section on Telegram.",
        )
    if telegram.bot_token is None or telegram.chat_id is None:
        missing = "BATON_TELEGRAM__BOT_TOKEN" if telegram.bot_token is None else "chat_id"
        return Check("telegram", Level.FAIL, f"{missing} is missing", "Set both, or neither.")
    try:
        name = await probes.telegram_bot_name(telegram)
    except Exception as err:  # noqa: BLE001 - any failure is the finding
        return Check(
            "telegram", Level.FAIL, f"bot not reachable ({type(err).__name__})", "Check the token."
        )
    if telegram.owner_id is None:
        return Check(
            "telegram",
            Level.WARN,
            f"@{name}, notices only",
            "Set [telegram] owner_id to your user id to approve, deny and answer from Telegram.",
        )
    return Check("telegram", Level.OK, f"@{name}, actions from user {telegram.owner_id}")


_MARKS = {Level.OK: "ok  ", Level.WARN: "warn", Level.FAIL: "FAIL"}


def render(checks: Sequence[Check]) -> str:
    width = max(len(check.name) for check in checks)
    lines = []
    for check in checks:
        lines.append(f"{_MARKS[check.level]}  {check.name:<{width}}  {check.detail}")
        if check.hint:
            lines.append(f"      {'':<{width}}  -> {check.hint}")
    return "\n".join(lines)
