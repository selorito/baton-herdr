"""Application settings.

Sources, highest precedence first:

1. keyword arguments passed to ``CobanSettings(...)``
2. environment variables (``COBAN_`` prefix, ``__`` between nested keys,
   e.g. ``COBAN_LOGGING__LEVEL=debug``)
3. the ``.env`` file in the working directory
4. the TOML file named by ``COBAN_CONFIG``, or ``./coban.toml``
5. defaults defined here

Secrets are typed as ``SecretStr`` so they never appear in ``repr``, logs or
serialized output. Keep them in ``.env`` or the environment, not in TOML.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

CONFIG_PATH_ENV_VAR = "COBAN_CONFIG"
DEFAULT_CONFIG_FILE = Path("coban.toml")

type LogLevel = Literal["debug", "info", "warning", "error", "critical"]


def _xdg_dir(env_var: str, fallback: str) -> Path:
    base = os.environ.get(env_var)
    return Path(base) if base else Path.home() / fallback


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class HerdrSettings(_Section):
    # herdr's own default: $XDG_CONFIG_HOME/herdr/herdr.sock
    socket_path: Path = Field(
        default_factory=lambda: _xdg_dir("XDG_CONFIG_HOME", ".config") / "herdr" / "herdr.sock"
    )

    @field_validator("socket_path")
    @classmethod
    def _expand_user(cls, value: Path) -> Path:
        return value.expanduser()


class DatabaseSettings(_Section):
    path: Path = Field(
        default_factory=lambda: _xdg_dir("XDG_DATA_HOME", ".local/share") / "coban" / "coban.db"
    )

    @field_validator("path")
    @classmethod
    def _expand_user(cls, value: Path) -> Path:
        return value.expanduser()


class LoggingSettings(_Section):
    level: LogLevel = "info"


class TelegramSettings(_Section):
    bot_token: SecretStr | None = None


class CobanSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="COBAN_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
        frozen=True,
    )

    herdr: HerdrSettings = Field(default_factory=HerdrSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    telegram: TelegramSettings = Field(default_factory=TelegramSettings)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        toml_file = Path(os.environ.get(CONFIG_PATH_ENV_VAR, DEFAULT_CONFIG_FILE))
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            TomlConfigSettingsSource(settings_cls, toml_file=toml_file),
            file_secret_settings,
        )
