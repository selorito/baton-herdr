"""The WSL1 start script (ADR 0022, amended), run with a fake herdr and a fake baton."""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import time
from typing import TYPE_CHECKING

import pytest

from baton_herdr.service import UnitPlan, install_start_script

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

pytestmark = pytest.mark.skipif(shutil.which("pgrep") is None, reason="needs pgrep")

# Each fake records how it was started, then keeps running like the real one.
FAKE = """\
#!/bin/bash
echo "$0 $* HERDR_CONFIG_PATH=${HERDR_CONFIG_PATH:-} SESSION=${BATON_HERDR__SESSION:-}" \\
    >>"$HOME/started"
sleep 60
"""


def wait_for(condition: Callable[[], bool], timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.1)
    pytest.fail("timed out")


def test_the_script_starts_herdr_then_batond_and_a_second_copy_leaves_them(
    tmp_path: Path,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("herdr", "baton"):
        (bin_dir / name).write_text(FAKE, encoding="utf-8")
        (bin_dir / name).chmod(0o755)
    config = tmp_path / "herdr-server.toml"
    plan = UnitPlan(
        herdr=str(bin_dir / "herdr"),
        baton=str(bin_dir / "baton"),
        session="baton-test",
        path=f"{bin_dir}:/usr/bin:/bin",
        lang="C.UTF-8",
        config=None,
        herdr_config=config,
    )
    script = tmp_path / "baton-wsl1-start"
    install_start_script(plan, force=False, path=script)
    env = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}
    started = tmp_path / "started"
    first = subprocess.Popen([str(script)], env=env, start_new_session=True)  # noqa: S603
    try:
        wait_for(lambda: started.exists() and len(started.read_text().splitlines()) == 2)
        herdr, baton = started.read_text().splitlines()
        assert herdr == (
            f"{bin_dir}/herdr --session baton-test server HERDR_CONFIG_PATH={config} SESSION="
        )
        assert baton == f"{bin_dir}/baton daemon HERDR_CONFIG_PATH= SESSION=baton-test"

        second = subprocess.Popen([str(script)], env=env, start_new_session=True)  # noqa: S603
        try:
            assert second.wait(timeout=10) == 0
        finally:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(second.pid, signal.SIGKILL)
        log = (tmp_path / ".local" / "state" / "baton" / "wsl1-start.log").read_text()
        assert log.splitlines()[-1].endswith("already running")
        assert first.poll() is None
        assert len(started.read_text().splitlines()) == 2
    finally:
        os.killpg(first.pid, signal.SIGKILL)
        first.wait()
