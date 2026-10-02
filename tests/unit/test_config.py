from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from baton_herdr.core import config as config_module
from baton_herdr.core.config import BatonSettings, config_file

# The real lookup; conftest points it at a temporary directory for every other test.
REAL_USER_CONFIG_DIR = config_module.user_config_dir

if TYPE_CHECKING:
    import pytest


def test_settings_layer_toml_env_and_dotenv_without_leaking_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = tmp_path / "custom.toml"
    config_file.write_text(
        '[herdr]\nsocket_path = "/run/herdr/herdr.sock"\n'
        '[database]\npath = "~/baton-test.db"\n'
        '[logging]\nlevel = "warning"\n',
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text("BATON_TELEGRAM__BOT_TOKEN=123:super-secret\n", encoding="utf-8")
    monkeypatch.setenv("BATON_CONFIG", str(config_file))
    monkeypatch.setenv("BATON_LOGGING__LEVEL", "debug")

    settings = BatonSettings()

    # From the TOML file named by BATON_CONFIG, with ~ expanded.
    assert settings.herdr.socket_path == Path("/run/herdr/herdr.sock")
    assert settings.database.path == Path.home() / "baton-test.db"
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
    user_dir = tmp_path / "xdg" / "baton"
    user_dir.mkdir(parents=True)
    (user_dir / "baton.toml").write_text('[logging]\nlevel = "error"\n', encoding="utf-8")
    (user_dir / ".env").write_text("BATON_TELEGRAM__CHAT_ID=5\n", encoding="utf-8")
    work = tmp_path / "elsewhere"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.delenv("BATON_CONFIG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(config_module, "user_config_dir", REAL_USER_CONFIG_DIR)

    assert config_file() == user_dir / "baton.toml"
    settings = BatonSettings()
    assert settings.logging.level == "error"
    assert settings.telegram.chat_id == 5

    # A baton.toml in the working directory takes precedence; so does ./.env.
    (work / "baton.toml").write_text('[logging]\nlevel = "warning"\n', encoding="utf-8")
    (work / ".env").write_text("BATON_TELEGRAM__CHAT_ID=6\n", encoding="utf-8")
    settings = BatonSettings()
    assert settings.logging.level == "warning"
    assert settings.telegram.chat_id == 6
