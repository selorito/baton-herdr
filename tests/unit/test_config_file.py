"""baton.toml as people's editors save it, and what baton says when it is not TOML."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from baton_herdr.cli import app
from baton_herdr.core.config import ConfigFileError, load_settings
from baton_herdr.doctor import Level, diagnose

if TYPE_CHECKING:
    from pathlib import Path


def test_a_config_saved_with_a_byte_order_mark_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "baton.toml"
    # As Windows PowerShell 5.1's Set-Content -Encoding utf8 writes it.
    config.write_bytes(b"\xef\xbb\xbf" + b'[scheduler]\ntimezone = "Europe/Istanbul"\n')
    monkeypatch.setenv("BATON_CONFIG", str(config))
    assert load_settings().scheduler.timezone == "Europe/Istanbul"


def test_a_config_that_is_not_toml_is_named_in_one_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "baton.toml"
    config.write_text("[scheduler\ntimezone = 3\n", encoding="utf-8")
    monkeypatch.setenv("BATON_CONFIG", str(config))
    with pytest.raises(ConfigFileError, match="not valid TOML"):
        load_settings()
    result = CliRunner().invoke(app, ["status"])
    assert result.exit_code == 2
    assert str(config) in result.stderr
    assert "Traceback" not in result.output


async def test_doctor_reports_a_broken_config_instead_of_failing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "baton.toml"
    config.write_bytes("timezone = 'İstanbul\n".encode("cp1254"))  # neither TOML nor UTF-8
    monkeypatch.setenv("BATON_CONFIG", str(config))
    (check,) = await diagnose()
    assert (check.name, check.level) == ("config", Level.FAIL)
