from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from coban.core.config import CobanSettings, config_file

if TYPE_CHECKING:
    import pytest


def test_settings_layer_toml_env_and_dotenv_without_leaking_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = tmp_path / "custom.toml"
    config_file.write_text(
        '[herdr]\nsocket_path = "/run/herdr/herdr.sock"\n'
        '[database]\npath = "~/coban-test.db"\n'
        '[logging]\nlevel = "warning"\n',
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text("COBAN_TELEGRAM__BOT_TOKEN=123:super-secret\n", encoding="utf-8")
    monkeypatch.setenv("COBAN_CONFIG", str(config_file))
    monkeypatch.setenv("COBAN_LOGGING__LEVEL", "debug")

    settings = CobanSettings()

    # From the TOML file named by COBAN_CONFIG, with ~ expanded.
    assert settings.herdr.socket_path == Path("/run/herdr/herdr.sock")
    assert settings.database.path == Path.home() / "coban-test.db"
    # Environment variable beats the TOML file.
    assert settings.logging.level == "debug"
    # Secret is loaded from .env but never rendered.
    assert settings.telegram.bot_token is not None
    assert settings.telegram.bot_token.get_secret_value() == "123:super-secret"
    assert "super-secret" not in repr(settings)
    assert "super-secret" not in settings.model_dump_json()


def test_without_a_local_file_the_users_config_dir_is_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_dir = tmp_path / "xdg" / "coban"
    user_dir.mkdir(parents=True)
    (user_dir / "coban.toml").write_text('[logging]\nlevel = "error"\n', encoding="utf-8")
    (user_dir / ".env").write_text("COBAN_TELEGRAM__CHAT_ID=5\n", encoding="utf-8")
    work = tmp_path / "elsewhere"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.delenv("COBAN_CONFIG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))

    assert config_file() == user_dir / "coban.toml"
    settings = CobanSettings()
    assert settings.logging.level == "error"
    assert settings.telegram.chat_id == 5

    # A coban.toml in the working directory takes precedence; so does ./.env.
    (work / "coban.toml").write_text('[logging]\nlevel = "warning"\n', encoding="utf-8")
    (work / ".env").write_text("COBAN_TELEGRAM__CHAT_ID=6\n", encoding="utf-8")
    settings = CobanSettings()
    assert settings.logging.level == "warning"
    assert settings.telegram.chat_id == 6
