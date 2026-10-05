"""Application settings.

Sources, highest precedence first:

1. keyword arguments passed to ``BatonSettings(...)``
2. environment variables (``BATON_`` prefix, ``__`` between nested keys,
   e.g. ``BATON_LOGGING__LEVEL=debug``)
3. the ``.env`` file in the working directory
4. the TOML file named by ``BATON_CONFIG``, or ``./baton.toml``
5. defaults defined here

Secrets are typed as ``SecretStr`` so they never appear in ``repr``, logs or
serialized output. Keep them in ``.env`` or the environment, not in TOML.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from pydantic_settings import (
    BaseSettings,
    DotEnvSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

CONFIG_PATH_ENV_VAR = "BATON_CONFIG"
DEFAULT_CONFIG_FILE = Path("baton.toml")
DOTENV_FILE = Path(".env")

type LogLevel = Literal["debug", "info", "warning", "error", "critical"]


def _xdg_dir(env_var: str, fallback: str) -> Path:
    base = os.environ.get(env_var)
    return Path(base) if base else Path.home() / fallback


def user_config_dir() -> Path:
    """``$XDG_CONFIG_HOME/baton``, or ``~/.config/baton``."""
    return _xdg_dir("XDG_CONFIG_HOME", ".config") / "baton"


def config_file() -> Path:
    """The TOML file settings are read from.

    ``BATON_CONFIG`` if set; else ``./baton.toml`` if it exists; else the user's
    ``~/.config/baton/baton.toml``, so the CLI and the service find the same file
    from any directory. A missing file means defaults.
    """
    explicit = os.environ.get(CONFIG_PATH_ENV_VAR)
    if explicit:
        return Path(explicit).expanduser()
    if DEFAULT_CONFIG_FILE.is_file():
        return DEFAULT_CONFIG_FILE
    return user_config_dir() / DEFAULT_CONFIG_FILE.name


def dotenv_files() -> tuple[Path, ...]:
    """Secrets files, later ones winning: the user's, then ``./.env``."""
    return (user_config_dir() / DOTENV_FILE.name, DOTENV_FILE)


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# The herdr session baton's panes live in; `baton service install` runs a herdr
# server for it. One default, so the service, batond, the CLI and doctor agree.
BATON_HERDR_SESSION = "baton"


class HerdrSettings(_Section):
    # An explicit socket overrides the session; see resolve_herdr_socket_path().
    socket_path: Path | None = None
    # "default" means herdr's own default socket (no named session).
    session: str | None = BATON_HERDR_SESSION

    @field_validator("session")
    @classmethod
    def _valid_session_name(cls, value: str | None) -> str | None:
        if value is not None and not is_valid_herdr_session_name(value):
            msg = f"invalid herdr session name: {value!r}"
            raise ValueError(msg)
        return value


# Mirrors herdr's socket lookup (herdr src/session.rs, src/config/io.rs):
#   1. HERDR_SOCKET_PATH (src/api/mod.rs SOCKET_PATH_ENV_VAR)
#   2. HERDR_SESSION=<name> -> <config_dir>/sessions/<name>/herdr.sock
#   3. <config_dir>/herdr.sock
# where <config_dir> is $XDG_CONFIG_HOME/herdr, else ~/.config/herdr.
# herdr injects HERDR_SOCKET_PATH into every pane it launches
# (src/integration/env.rs apply_pane_base_env) and into plugin panes
# (src/app/api/plugins/panes.rs), so when baton runs inside herdr, step 1 is
# what points it at the right server.
# Not mirrored: debug herdr builds use "herdr-dev" instead of "herdr".
HERDR_SOCKET_PATH_ENV_VAR = "HERDR_SOCKET_PATH"
HERDR_SESSION_ENV_VAR = "HERDR_SESSION"
HERDR_DEFAULT_SESSION_NAME = "default"
_HERDR_SOCKET_FILE = "herdr.sock"
_HERDR_SESSION_NAME_MAX_BYTES = 64
_HERDR_SESSION_NAME = re.compile(r"[A-Za-z0-9._-]+")


def is_valid_herdr_session_name(name: str) -> bool:
    """Same rules as herdr's ``session::validate_name``."""
    return (
        len(name.encode()) <= _HERDR_SESSION_NAME_MAX_BYTES
        and name not in {".", ".."}
        and _HERDR_SESSION_NAME.fullmatch(name) is not None
    )


def resolve_herdr_socket_path(
    settings: HerdrSettings, *, env: Mapping[str, str], home: Path
) -> Path:
    """Return the herdr API socket baton should connect to.

    Precedence: ``[herdr].socket_path`` > ``HERDR_SOCKET_PATH`` >
    ``[herdr].session`` > ``HERDR_SESSION`` > herdr's default socket.

    Pure: the environment and home directory are passed in. Empty environment
    values count as unset.
    """
    if settings.socket_path is not None:
        return _expand_home(settings.socket_path, home)
    if socket_from_env := env.get(HERDR_SOCKET_PATH_ENV_VAR):
        return Path(socket_from_env)

    xdg_config_home = env.get("XDG_CONFIG_HOME")
    config_dir = (Path(xdg_config_home) if xdg_config_home else home / ".config") / "herdr"

    session = settings.session or env.get(HERDR_SESSION_ENV_VAR)
    # Like herdr's session::active_name: "default" and invalid names mean no session.
    if session and session != HERDR_DEFAULT_SESSION_NAME and is_valid_herdr_session_name(session):
        return config_dir / "sessions" / session / _HERDR_SOCKET_FILE
    return config_dir / _HERDR_SOCKET_FILE


def _expand_home(path: Path, home: Path) -> Path:
    if path.parts and path.parts[0] == "~":
        return home.joinpath(*path.parts[1:])
    return path


class DatabaseSettings(_Section):
    path: Path = Field(
        default_factory=lambda: _xdg_dir("XDG_DATA_HOME", ".local/share") / "baton" / "baton.db"
    )

    @field_validator("path")
    @classmethod
    def _expand_user(cls, value: Path) -> Path:
        return value.expanduser()


class LoggingSettings(_Section):
    level: LogLevel = "info"


class SchedulerSettings(_Section):
    # Agents to use, in order of preference. A task moves to the next available one
    # when the current one hits its usage limit.
    agents: tuple[Literal["claude", "codex", "opencode"], ...] = ("claude", "codex")
    # How long an agent stays unavailable after a limit that printed no reset time.
    limit_cooldown_minutes: int = Field(default=60, ge=1)
    start_timeout_seconds: float = Field(default=120, gt=0)
    turn_timeout_seconds: float = Field(default=3600, gt=0)
    poll_interval_seconds: float = Field(default=2, gt=0)
    # IANA zone for reading clock times that agents print, e.g. "resets 3:45pm".
    timezone: str = "UTC"
    # Replace an agent's launch command, e.g. {"claude": "claude --permission-mode default"}.
    launch_commands: dict[Literal["claude", "codex", "opencode"], str] = Field(default_factory=dict)
    # Automatic resumes of a task after its agent crashed or stalled (ADR 0005).
    max_failure_resumes: int = Field(default=2, ge=0)


class UsageSettings(_Section):
    """The usage collector, `baton-detect usage` (ADR 0011)."""

    enabled: bool = True
    # A name on PATH or a path; `cargo install --path crates/baton-detect` puts it in
    # ~/.cargo/bin.
    binary: str = "baton-detect"
    # The agents' log locations; unset means baton-detect's defaults (the agents' own).
    claude_dir: Path | None = None
    codex_dir: Path | None = None
    opencode_db: Path | None = None
    # How far before the newest stored record reading starts again after a restart.
    # Repeats are dropped by the database, so this only needs to be generous.
    reread_minutes: int = Field(default=60, ge=0)

    @field_validator("claude_dir", "codex_dir", "opencode_db")
    @classmethod
    def _expand_user(cls, value: Path | None) -> Path | None:
        return value.expanduser() if value else None


class DetectorSettings(_Section):
    """Which detector classifies screens (ADR 0011, phase 2).

    ``python``: the adapters' rules. ``shadow``: Python decides, and the Rust classifier
    (`baton-detect classify`) answers every screen too; differences are logged as
    "detector mismatch". ``rust``: the Rust classifier decides, Python answers while it
    is unavailable.
    """

    engine: Literal["python", "shadow", "rust"] = "python"
    # A name on PATH or a path; the same program as [usage] binary.
    binary: str = "baton-detect"
    # How long one answer may take before the process is restarted.
    timeout_seconds: float = Field(default=5, gt=0)


class BudgetSettings(_Section):
    """Remaining budget per agent and how it steers agent choice (ADR 0012)."""

    # An agent with less than this share of its budget left is chosen only when no
    # other available agent has more. Unknown budgets never count as low.
    reserve_percent: float = Field(default=10, ge=0, le=100)
    # Claude Code does not report its limits. Its 5-hour session budget, in counted
    # tokens (input + cache writes + output), turns its usage into an estimate. Unset:
    # learned from the last session limit baton saw, if any.
    claude_window_tokens: int | None = Field(default=None, gt=0)


class TelegramSettings(_Section):
    bot_token: SecretStr | None = None
    # The only chat baton writes to. Without it no message is sent.
    chat_id: int | None = None
    # The only Telegram user whose commands and buttons are handled (ADR 0009).
    # Without it the bot only sends notices.
    owner_id: int | None = None


class BatonSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="BATON_",
        env_nested_delimiter="__",
        env_file_encoding="utf-8",
        extra="forbid",
        frozen=True,
    )

    herdr: HerdrSettings = Field(default_factory=HerdrSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    scheduler: SchedulerSettings = Field(default_factory=SchedulerSettings)
    telegram: TelegramSettings = Field(default_factory=TelegramSettings)
    usage: UsageSettings = Field(default_factory=UsageSettings)
    budget: BudgetSettings = Field(default_factory=BudgetSettings)
    detector: DetectorSettings = Field(default_factory=DetectorSettings)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        del dotenv_settings  # replaced: the files are looked up when settings are read
        return (
            init_settings,
            env_settings,
            DotEnvSettingsSource(settings_cls, env_file=dotenv_files()),
            TomlConfigSettingsSource(settings_cls, toml_file=config_file()),
            file_secret_settings,
        )
