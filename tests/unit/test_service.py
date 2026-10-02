from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from coban.service import (
    COBAN_UNIT,
    HERDR_UNIT,
    ServiceError,
    UnitPlan,
    install_units,
    render_units,
    search_path,
)

PLAN = UnitPlan(
    herdr="/home/u/.local/bin/herdr",
    coban="/home/u/.local/bin/coban",
    session="coban",
    path="/home/u/.local/bin:/usr/bin",
    lang="C.UTF-8",
    config=None,
)


def test_units_run_herdr_in_its_own_session_and_coban_against_it() -> None:
    units = render_units(PLAN)
    herdr, coban = units[HERDR_UNIT], units[COBAN_UNIT]
    assert "ExecStart=/home/u/.local/bin/herdr --session coban server\n" in herdr
    assert f"Requires={HERDR_UNIT}\nAfter={HERDR_UNIT}\n" in coban
    assert "ExecStart=/home/u/.local/bin/coban daemon\n" in coban
    assert 'Environment="COBAN_HERDR__SESSION=coban"\n' in coban
    assert 'Environment="PATH=/home/u/.local/bin:/usr/bin"\n' in herdr
    assert "COBAN_CONFIG" not in coban  # the user default needs no pin
    pinned = render_units(replace(PLAN, config=Path("/srv/coban.toml")))
    assert 'Environment="COBAN_CONFIG=/srv/coban.toml"\n' in pinned[COBAN_UNIT]


def test_path_puts_the_found_binaries_first_without_duplicates() -> None:
    assert search_path(["/a/bin/herdr", None, "/a/bin/claude", "/b/coban"]) == (
        "/a/bin:/b:/usr/local/bin:/usr/bin:/bin"
    )


def test_install_writes_both_units_and_never_overwrites_silently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    written = install_units(PLAN, force=False)
    assert sorted(p.name for p in written) == sorted([COBAN_UNIT, HERDR_UNIT])
    assert all(p.parent == tmp_path / "systemd" / "user" for p in written)

    with pytest.raises(ServiceError, match="--force"):
        install_units(PLAN, force=False)
    assert install_units(PLAN, force=True) == written
