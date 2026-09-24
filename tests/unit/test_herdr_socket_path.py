from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from coban.core.config import HerdrSettings, resolve_herdr_socket_path

HOME = Path("/home/tester")
DEFAULT_SOCKET = HOME / ".config" / "herdr" / "herdr.sock"


def resolve(env: dict[str, str], **settings: str) -> Path:
    return resolve_herdr_socket_path(HerdrSettings.model_validate(settings), env=env, home=HOME)


def test_explicit_socket_path_beats_every_environment_variable() -> None:
    env = {"HERDR_SOCKET_PATH": "/run/from-env.sock", "HERDR_SESSION": "work"}

    assert resolve(env, socket_path="/run/explicit.sock", session="other") == Path(
        "/run/explicit.sock"
    )


def test_explicit_socket_path_expands_the_injected_home() -> None:
    assert resolve({}, socket_path="~/sockets/herdr.sock") == HOME / "sockets" / "herdr.sock"


def test_herdr_socket_path_env_beats_session_settings() -> None:
    # herdr injects HERDR_SOCKET_PATH into every pane and plugin it starts.
    env = {"HERDR_SOCKET_PATH": "/run/injected.sock", "HERDR_SESSION": "work"}

    assert resolve(env, session="other") == Path("/run/injected.sock")


def test_configured_session_beats_herdr_session_env() -> None:
    assert (
        resolve({"HERDR_SESSION": "work"}, session="other")
        == HOME / ".config" / "herdr" / "sessions" / "other" / "herdr.sock"
    )


def test_herdr_session_env_selects_named_session_socket() -> None:
    assert (
        resolve({"HERDR_SESSION": "work"})
        == HOME / ".config" / "herdr" / "sessions" / "work" / "herdr.sock"
    )


@pytest.mark.parametrize("session", ["default", "", "../escape", "has space", "x" * 65])
def test_default_or_invalid_herdr_session_env_falls_back_to_default_socket(session: str) -> None:
    assert resolve({"HERDR_SESSION": session}) == DEFAULT_SOCKET


def test_default_socket_uses_xdg_config_home() -> None:
    env = {"XDG_CONFIG_HOME": "/xdg/config"}

    assert resolve(env) == Path("/xdg/config/herdr/herdr.sock")
    assert resolve(env | {"HERDR_SESSION": "work"}) == Path(
        "/xdg/config/herdr/sessions/work/herdr.sock"
    )


def test_default_socket_without_any_environment() -> None:
    assert resolve({}) == DEFAULT_SOCKET
    assert resolve({"HERDR_SOCKET_PATH": "", "XDG_CONFIG_HOME": ""}) == DEFAULT_SOCKET


@pytest.mark.parametrize("session", ["..", "a/b", "x" * 65])
def test_invalid_configured_session_is_rejected(session: str) -> None:
    with pytest.raises(ValidationError, match="invalid herdr session name"):
        HerdrSettings(session=session)
