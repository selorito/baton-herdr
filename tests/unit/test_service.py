from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from baton_herdr.core.config import BatonSettings, HerdrSettings
from baton_herdr.service import (
    BATON_UNIT,
    HERDR_UNIT,
    ServiceError,
    UnitPlan,
    install_units,
    installed_session,
    render_units,
    search_path,
    service_session,
)

PLAN = UnitPlan(
    herdr="/home/u/.local/bin/herdr",
    baton="/home/u/.local/bin/baton",
    session="baton",
    path="/home/u/.local/bin:/usr/bin",
    lang="C.UTF-8",
    config=None,
)


def test_units_run_herdr_in_its_own_session_and_baton_against_it() -> None:
    units = render_units(PLAN)
    herdr, baton = units[HERDR_UNIT], units[BATON_UNIT]
    assert "ExecStart=/home/u/.local/bin/herdr --session baton server\n" in herdr
    assert f"Requires={HERDR_UNIT}\nAfter={HERDR_UNIT}\n" in baton
    assert "ExecStart=/home/u/.local/bin/baton daemon\n" in baton
    assert 'Environment="BATON_HERDR__SESSION=baton"\n' in baton
    assert 'Environment="PATH=/home/u/.local/bin:/usr/bin"\n' in herdr
    assert "BATON_CONFIG" not in baton  # the user default needs no pin
    pinned = render_units(replace(PLAN, config=Path("/srv/baton.toml")))
    assert 'Environment="BATON_CONFIG=/srv/baton.toml"\n' in pinned[BATON_UNIT]


def test_path_puts_the_found_binaries_first_without_duplicates() -> None:
    assert search_path(["/a/bin/herdr", None, "/a/bin/claude", "/b/baton"]) == (
        "/a/bin:/b:/usr/local/bin:/usr/bin:/bin"
    )


def test_install_writes_both_units_and_never_overwrites_silently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    written = install_units(PLAN, force=False)
    assert sorted(p.name for p in written) == sorted([BATON_UNIT, HERDR_UNIT])
    assert all(p.parent == tmp_path / "systemd" / "user" for p in written)

    with pytest.raises(ServiceError, match="--force"):
        install_units(PLAN, force=False)
    assert install_units(PLAN, force=True) == written


def test_unit_names_follow_adr_0010() -> None:
    assert sorted(render_units(PLAN)) == ["baton-herdr.service", "baton.service"]


def test_the_service_runs_the_configured_session_and_nothing_else() -> None:
    assert service_session(BatonSettings()) == "baton"
    assert service_session(BatonSettings(herdr=HerdrSettings(session="work"))) == "work"
    with pytest.raises(ServiceError, match="socket_path"):
        service_session(BatonSettings(herdr=HerdrSettings(socket_path=Path("/run/h.sock"))))
    for unnamed in (None, "default"):
        with pytest.raises(ServiceError, match="session of its own"):
            service_session(BatonSettings(herdr=HerdrSettings(session=unnamed)))


def test_the_installed_units_session_is_read_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert installed_session() is None
    install_units(replace(PLAN, session="work"), force=False)
    assert installed_session() == "work"
