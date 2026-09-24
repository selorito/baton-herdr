from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from coban.core.config import CobanSettings

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
