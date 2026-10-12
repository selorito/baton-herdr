"""``baton setup``: from a fresh machine to a running batond, in one guided pass.

Each step checks first and changes only what is missing, so running it again is safe: an
existing config, policy or service is kept unless the user says otherwise. Secrets are
never printed: the bot token is read without echo and written to ``.env`` with mode 600.

The pure parts (editing a TOML value in place, the files' first contents, finding the
owner in Telegram's updates) are here and tested on their own; ``run_setup`` drives them
through an ``Io`` of prompts, commands and HTTP, which tests replace.
"""

from __future__ import annotations

import getpass
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

# The agents baton drives, in the order they are preferred by default.
AGENT_ORDER = ("claude", "codex", "opencode")
RELEASE_URL = (
    "https://github.com/selorito/baton-herdr/releases/latest/download/baton-detect-{target}"
)
TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"


class SetupError(Exception):
    """A step cannot go on; the message says what to do."""


def set_toml_value(text: str, section: str, key: str, value: str) -> str:
    """``text`` with ``key = value`` in ``[section]``: replaced where the key is set (or set
    commented out), added under the section otherwise, the section added at the end if it
    is missing. ``value`` is TOML already (``"x"``, ``5``, ``["a"]``). Comments and every
    other line are kept."""
    lines = text.splitlines()
    header = re.compile(r"^\s*\[([^\]]+)\]\s*(#.*)?$")
    start = next(
        (i for i, line in enumerate(lines) if (m := header.match(line)) and m[1] == section),
        None,
    )
    if start is None:
        body = text if text.endswith("\n") or not text else text + "\n"
        gap = "\n" if body else ""
        return f"{body}{gap}[{section}]\n{key} = {value}\n"
    end = next((i for i in range(start + 1, len(lines)) if header.match(lines[i])), len(lines))
    setting = re.compile(rf"^\s*#?\s*{re.escape(key)}\s*=")
    for i in range(start + 1, end):
        if setting.match(lines[i]) and not lines[i].lstrip().startswith("#"):
            lines[i] = f"{key} = {value}"
            return "\n".join(lines) + "\n"
    for i in range(start + 1, end):
        if setting.match(lines[i]):  # a commented-out default: set it there
            lines[i] = f"{key} = {value}"
            return "\n".join(lines) + "\n"
    lines.insert(start + 1, f"{key} = {value}")
    return "\n".join(lines) + "\n"


def first_config(agents: Sequence[str], timezone: str, *, phone_preview: bool) -> str:
    """baton.toml for a new install: the agents found, in preference order, and the zone."""
    listed = ", ".join(f'"{agent}"' for agent in agents)
    host = "0.0.0.0" if phone_preview else "127.0.0.1"  # noqa: S104 - the user asked for it
    return f"""\
# baton's settings, written by baton setup. Every key is explained in baton.example.toml.
[scheduler]
# Agents in order of preference; a task moves to the next one when one hits its limit.
agents = [{listed}]
# The zone agents print their limits' reset times in: this machine's.
timezone = "{timezone}"

[projects]
# Where /new makes projects. 0.0.0.0 lets a phone on this network open the preview links.
preview_host = "{host}"

[telegram]
"""


def first_policy(trusted: Sequence[str]) -> str:
    """policy.yaml for a new install: the directories whose code agents may test and build."""
    listed = "".join(f"  - {directory}\n" for directory in trusted)
    return (
        "# Which agent commands baton approves without asking (ADR 0013); see\n"
        "# policy.example.yaml. Agents may run tests and builds without asking only in\n"
        "# trusted_dirs: list only directories whose code you trust.\n"
        + ("trusted_dirs:\n" + listed if trusted else "trusted_dirs: []\n")
        + "rules: []\n"
    )


@dataclass(frozen=True, slots=True)
class Owner:
    chat_id: int
    user_id: int
    name: str


def find_owner(updates: dict[str, Any]) -> Owner | None:
    """The newest private message to the bot in ``getUpdates``'s answer: its chat and
    sender. A group's message is never taken: baton talks to one person."""
    for update in reversed(updates.get("result", [])):
        message = update.get("message") or {}
        chat, sender = message.get("chat") or {}, message.get("from") or {}
        if chat.get("type") == "private" and "id" in chat and "id" in sender:
            name = sender.get("username") or sender.get("first_name") or str(sender["id"])
            return Owner(chat_id=int(chat["id"]), user_id=int(sender["id"]), name=str(name))
    return None


def telegram_call(token: str, method: str, *, timeout: float = 30, **params: object) -> Any:
    """One Bot API call; raises ``SetupError`` with Telegram's own description."""
    url = TELEGRAM_API.format(token=token, method=method)
    if params:
        url += "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - https only
            body = json.load(response)
    except urllib.error.HTTPError as err:
        body = json.load(err)
    except (urllib.error.URLError, TimeoutError, OSError) as err:
        msg = f"Telegram could not be reached: {err}"
        raise SetupError(msg) from err
    if not body.get("ok"):
        msg = f"Telegram refused {method}: {body.get('description', 'no reason given')}"
        raise SetupError(msg)
    return body["result"]


@dataclass(frozen=True, slots=True)
class Io:
    """What setup does outside itself; tests replace it."""

    say: Callable[[str], None]
    ask: Callable[[str, str], str]  # (question, default) -> answer
    confirm: Callable[..., bool]  # (question, *, default) -> yes
    secret: Callable[[str], str]  # a question whose answer is not echoed
    run: Callable[[Sequence[str]], tuple[int, str]]  # a command -> (exit status, output)
    which: Callable[[str], str | None]
    telegram: Callable[..., Any] = telegram_call
    sleep: Callable[[float], None] = time.sleep


def wait_for_owner(io: Io, token: str, *, tries: int = 40) -> Owner | None:
    """Poll the bot until someone writes to it in private (about two minutes)."""
    offset = 0
    for _ in range(tries):
        updates = io.telegram(token, "getUpdates", timeout=10, offset=offset)
        if owner := find_owner({"result": updates}):
            return owner
        if updates:
            offset = max(u["update_id"] for u in updates) + 1
        io.sleep(3)
    return None


def write_private(path: Path, text: str) -> None:
    """A file only its owner can read (a secret)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    path.write_text(text, encoding="utf-8")


def set_env_value(text: str, key: str, value: str) -> str:
    """``.env`` text with ``key=value`` set, other lines kept."""
    lines = [line for line in text.splitlines() if not line.startswith(f"{key}=")]
    return "\n".join([*lines, f"{key}={value}"]) + "\n"


@dataclass(frozen=True, slots=True)
class Paths:
    config: Path  # baton.toml
    env: Path  # .env: the bot token
    policy: Path  # policy.yaml
    projects_root: Path
    local_bin: Path  # where a downloaded baton-detect goes


def run_setup(io: Io, paths: Paths, *, timezone: str, target: str) -> bool:
    """Every step in order. Returns False when a step was left for later (it says which)."""
    complete = True
    if io.which("herdr") is None:
        msg = "herdr is not installed. Install it first: https://github.com/herdrdev/herdr"
        raise SetupError(msg)
    agents = [agent for agent in AGENT_ORDER if io.which(agent)]
    if not agents:
        msg = "No agent is installed (claude, codex, opencode). Install one and sign in first."
        raise SetupError(msg)
    io.say(f"Agents found: {', '.join(agents)}.")
    complete &= _detector(io, paths, target)
    _integrations(io, agents)
    _config(io, paths, agents, timezone)
    complete &= _telegram(io, paths)
    _policy(io, paths)
    return complete


def _detector(io: Io, paths: Paths, target: str) -> bool:
    if io.which("baton-detect"):
        return True
    destination = paths.local_bin / "baton-detect"
    if not io.confirm(
        f"baton-detect (baton's screen reader) is missing. Download it into {destination}?",
        default=True,
    ):
        io.say("Install it later: cargo install --path crates/baton-detect --locked")
        return False
    paths.local_bin.mkdir(parents=True, exist_ok=True)
    url = RELEASE_URL.format(target=target)
    status, output = io.run(["curl", "-fsSL", "-o", str(destination), url])
    if status != 0:
        io.say(f"The download failed ({output.strip()}). Install it later from {url}.")
        return False
    destination.chmod(0o755)
    io.say(f"Installed {destination}.")
    return True


def _integrations(io: Io, agents: Sequence[str]) -> None:
    """herdr's hooks, which tell baton each agent's session id."""
    from baton_herdr.doctor import parse_integration_status  # noqa: PLC0415 - one parser

    _, output = io.run(["herdr", "integration", "status"])
    status = parse_integration_status(output)
    for agent in agents:
        if status.get(agent, "").startswith("current"):
            continue
        code, out = io.run(["herdr", "integration", "install", agent])
        io.say(f"herdr integration for {agent}: {'installed' if code == 0 else out.strip()}")


def _config(io: Io, paths: Paths, agents: Sequence[str], timezone: str) -> None:
    if paths.config.exists():
        io.say(f"Keeping {paths.config}.")
        return
    phone = io.confirm(
        "Open project preview links from your phone on this network? Anyone on the network "
        "could then read the projects' pages.",
        default=False,
    )
    paths.config.parent.mkdir(parents=True, exist_ok=True)
    paths.config.write_text(first_config(agents, timezone, phone_preview=phone), encoding="utf-8")
    io.say(f"Wrote {paths.config}: agents {', '.join(agents)}, time zone {timezone}.")


def _telegram(io: Io, paths: Paths) -> bool:
    if not io.confirm("Set up Telegram, for notices and approvals on your phone?", default=True):
        return True
    env = paths.env.read_text(encoding="utf-8") if paths.env.exists() else ""
    known = re.search(r"^BATON_TELEGRAM__BOT_TOKEN=(.+)$", env, re.MULTILINE)
    if known and not io.confirm("A bot token is already set. Replace it?", default=False):
        token = known[1].strip()
    else:
        io.say("Create a bot with @BotFather in Telegram (/newbot) and paste its token here.")
        token = io.secret("Bot token (not shown)").strip()
    me = io.telegram(token, "getMe")
    if not known or known[1].strip() != token:
        write_private(paths.env, set_env_value(env, "BATON_TELEGRAM__BOT_TOKEN", token))
        io.say(f"Saved the token in {paths.env}, readable only by you.")
    io.say(f"Now send any message to @{me['username']} from your phone. Waiting two minutes...")
    owner = wait_for_owner(io, token)
    if owner is None:
        io.say("No message arrived. Run baton setup again to finish Telegram.")
        return False
    text = paths.config.read_text(encoding="utf-8") if paths.config.exists() else ""
    text = set_toml_value(text, "telegram", "chat_id", str(owner.chat_id))
    text = set_toml_value(text, "telegram", "owner_id", str(owner.user_id))
    paths.config.write_text(text, encoding="utf-8")
    io.say(f"Telegram: notices go to {owner.name}, and only {owner.name} can act on them.")
    return True


def _policy(io: Io, paths: Paths) -> None:
    if paths.policy.exists():
        io.say(f"Keeping {paths.policy}.")
        return
    answer = io.ask(
        "Directories whose code agents may test and build without asking, comma separated "
        "(your own repositories; blank for none)",
        "",
    )
    trusted = [d.strip() for d in answer.split(",") if d.strip()]
    trusted.append(str(paths.projects_root))  # baton makes those itself (ADR 0016)
    paths.policy.parent.mkdir(parents=True, exist_ok=True)
    paths.policy.write_text(first_policy(trusted), encoding="utf-8")
    io.say(f"Wrote {paths.policy}: trusted {', '.join(trusted)}.")


def ask_secret(question: str) -> str:
    return getpass.getpass(f"{question}: ")
