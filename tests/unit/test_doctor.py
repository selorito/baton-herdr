from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from baton_herdr.doctor import Check, Level, Probes, diagnose, parse_integration_status, render

if TYPE_CHECKING:
    from collections.abc import Sequence

    import pytest

    from baton_herdr.core.config import HerdrSettings, TelegramSettings

STATUS = """\
claude: current (v10) (/home/u/.claude/hooks/herdr-agent-state.sh)
codex: outdated (v7, current v8) (/home/u/.codex/herdr-agent-state.sh)
opencode: not installed (/home/u/.config/opencode/plugins/herdr-agent-state.js)
letta (experimental): not installed (/home/u/.letta/hooks/herdr-agent-session.sh)
"""


def test_integration_status_lines_are_parsed() -> None:
    assert parse_integration_status(STATUS) == {
        "claude": "current (v10)",
        "codex": "outdated (v7, current v8)",
        "opencode": "not installed",
        "letta": "not installed",
    }


def probes(
    *,
    herdr_up: bool = True,
    on_path: Sequence[str] = ("herdr", "claude"),
    detector: str | None = "baton-detect 0.1.0",
    classified: str = (
        '{"contract":1,"state":"rate_limited","evidence":"baton:claude_usage_limit",'
        '"resets_at":"2026-10-01T12:45:00Z"}'
    ),
) -> Probes:
    async def ping(_: HerdrSettings) -> str:
        if not herdr_up:
            raise ConnectionRefusedError
        return "/run/herdr.sock"

    async def bot_name(_: TelegramSettings) -> str:
        return "baton_bot"

    async def count(_: Path) -> int:
        return 3

    def run(command: Sequence[str]) -> str:
        if "integration" in command:
            return STATUS
        if command[0].endswith("baton-detect"):
            return f"{detector}\n"
        return "herdr 0.9.1\n"

    return Probes(
        which=lambda name: f"/bin/{name}" if name in on_path else None,
        run=run,
        ping_herdr=ping,
        telegram_bot_name=bot_name,
        count_events=count,
        local_timezone=lambda: "Europe/Istanbul",
        installed_session=lambda: None,  # never this machine's units
        environ=dict,
        detector=lambda name: Path(f"/opt/{name}") if detector else None,
        classify=lambda _: classified,
        package_version=lambda: "0.1.0",
    )


def by_name(checks: Sequence[Check]) -> dict[str, Check]:
    return {check.name: check for check in checks}


async def test_a_healthy_setup_with_one_outdated_integration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "baton.toml"
    config.write_text(
        '[scheduler]\nagents = ["claude", "codex"]\ntimezone = "Europe/Istanbul"\n'
        "[telegram]\nchat_id = 5\nowner_id = 7\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("BATON_CONFIG", str(config))
    monkeypatch.setenv("BATON_TELEGRAM__BOT_TOKEN", "1:x")
    (tmp_path / "baton.db").touch()
    monkeypatch.setenv("BATON_DATABASE__PATH", str(tmp_path / "baton.db"))

    checks = by_name(await diagnose(probes(on_path=("herdr", "claude", "codex"))))

    assert {name: c.level for name, c in checks.items() if c.level is not Level.OK} == {
        "codex integration": Level.FAIL,
        "policy": Level.WARN,  # no policy.yaml: the defaults, said out loud
    }
    assert "herdr integration install codex" in checks["codex integration"].hint
    assert checks["telegram"].detail == "@baton_bot, actions from user 7"
    assert "FAIL  codex integration" in render(list(checks.values()))


async def test_missing_pieces_are_named_with_a_fix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BATON_CONFIG", str(tmp_path / "missing.toml"))
    monkeypatch.setenv("BATON_SCHEDULER__AGENTS", '["claude", "codex"]')
    monkeypatch.setenv("BATON_DATABASE__PATH", str(tmp_path / "new" / "baton.db"))

    checks = by_name(await diagnose(probes(herdr_up=False)))

    assert checks["config"].level is Level.WARN
    assert checks["database"].detail.endswith("(created on first use)")
    assert not (tmp_path / "new").exists()
    assert checks["herdr server"].level is Level.FAIL
    assert "baton service install" in checks["herdr server"].hint
    assert checks["codex"].level is Level.FAIL  # not on PATH
    assert checks["timezone"].level is Level.WARN  # UTC on a machine in Istanbul
    assert 'timezone = "Europe/Istanbul"' in checks["timezone"].hint
    assert checks["telegram"].level is Level.WARN


async def test_an_invalid_config_stops_the_other_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "baton.toml"
    config.write_text("[scheduler]\npoll_interval_seconds = -1\n", encoding="utf-8")
    monkeypatch.setenv("BATON_CONFIG", str(config))

    (check,) = await diagnose(probes())

    assert check.level is Level.FAIL
    assert "scheduler.poll_interval_seconds" in check.detail


async def test_doctor_names_a_service_session_the_settings_do_not_reach(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "baton.toml"
    config.write_text('[herdr]\nsession = "default"\n', encoding="utf-8")
    monkeypatch.setenv("BATON_CONFIG", str(config))
    base = probes()

    def with_service(**env: str) -> Probes:
        return replace(base, installed_session=lambda: "baton", environ=lambda: env)

    checks = by_name(await diagnose(with_service()))
    service = checks["service session"]
    assert service.level is Level.FAIL
    assert "the service runs herdr session 'baton'" in service.detail
    assert "session = 'default'" in service.detail
    assert 'Set [herdr] session = "baton"' in service.hint

    # Inside a herdr pane, HERDR_SOCKET_PATH wins over the setting.
    config.write_text('[herdr]\nsession = "baton"\n', encoding="utf-8")
    inside = by_name(await diagnose(with_service(HERDR_SOCKET_PATH="/run/other.sock")))
    assert "HERDR_SOCKET_PATH" in inside["service session"].detail
    assert "env -u HERDR_SOCKET_PATH" in inside["service session"].hint

    # Matching: one line, ok; without an installed service: no line.
    assert by_name(await diagnose(with_service()))["service session"].level is Level.OK
    assert "service session" not in by_name(await diagnose(base))


async def test_baton_detect_is_checked_unless_usage_collection_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert by_name(await diagnose(probes()))["baton-detect"].level is Level.OK

    missing = by_name(await diagnose(probes(detector=None)))["baton-detect"]
    assert missing.level is Level.FAIL
    assert "cargo install --path crates/baton-detect" in missing.hint

    stale = by_name(await diagnose(probes(detector="baton-detect 0.0.9")))["baton-detect"]
    assert stale.level is Level.WARN
    assert "but baton is 0.1.0" in stale.detail

    monkeypatch.setenv("BATON_USAGE__ENABLED", "false")
    off = by_name(await diagnose(probes(detector=None)))["baton-detect"]
    assert off.level is Level.OK
    assert "off" in off.detail


async def test_the_classifier_must_run_and_read_a_test_screen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rust = by_name(await diagnose(probes()))["detector"]
    assert (rust.level, rust.detail) == (Level.OK, "/opt/baton-detect")

    missing = by_name(await diagnose(probes(detector=None)))["detector"]
    assert missing.level is Level.FAIL
    assert missing.detail.endswith("limits and prompt kinds go unrecognised")
    misread = '{"contract":1,"state":"idle","evidence":"host:no-evidence","resets_at":null}'
    wrong = by_name(await diagnose(probes(classified=misread)))["detector"]
    assert wrong.level is Level.FAIL
    assert "misreads a test screen (host:no-evidence)" in wrong.detail
    broken = by_name(await diagnose(probes(classified="Usage: baton-detect")))["detector"]
    assert "does not classify (ValidationError)" in broken.detail

    # The switch-over's modes still load, run as rust, and are named for removal.
    monkeypatch.setenv("BATON_DETECTOR__ENGINE", "shadow")
    shadow = by_name(await diagnose(probes()))["detector"]
    assert shadow.level is Level.WARN
    assert 'engine = "shadow" runs as "rust"' in shadow.detail


async def test_a_broken_policy_file_fails_before_batond_would(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BATON_CONFIG", str(tmp_path / "missing.toml"))
    monkeypatch.setenv("BATON_DATABASE__PATH", str(tmp_path / "baton.db"))
    checks = by_name(await diagnose(probes()))
    assert checks["policy"].level is Level.WARN
    assert "read-only commands approved without asking" in checks["policy"].detail
    assert "New in 1.0" in checks["policy"].hint

    policy = tmp_path / "policy.yaml"
    policy.write_text("rules: [{decision: allow}]\n", encoding="utf-8")
    monkeypatch.setenv("BATON_POLICY__PATH", str(policy))
    checks = by_name(await diagnose(probes()))
    assert checks["policy"].level is Level.FAIL
    assert "give either command or regex" in checks["policy"].detail

    policy.write_text('rules: [{decision: deny, command: "terraform *"}]\n', encoding="utf-8")
    checks = by_name(await diagnose(probes()))
    assert checks["policy"].level is Level.OK
    assert checks["policy"].detail == (
        f"{policy}: read-only commands approved without asking; tests and builds always "
        "asked (no trusted_dirs); 1 own rule first"
    )
