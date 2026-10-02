#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""A scripted stand-in for a coding agent, for demos and live tests. Spends no quota.

It prints screens shaped like the real agent's (see ``fixtures/``). Inside a herdr
pane it is meant to run under the agent's own name, through a launcher made with
``--make-launcher``: herdr then recognises the process as that agent and derives
its state from the screens with its own detection rules, exactly as for the real
agent. Like the real agent's herdr hook, the fake reports only its session id,
using the official integration source name ``herdr:<agent>`` (herdr exposes
session ids only from those sources). Use it in throwaway herdr sessions only.

    fake_agent.py --make-launcher DIR claude      # writes DIR/claude
    DIR/claude tools/fake-agent/scripts/claude-limit.toml [--session-id ID]

A script is a TOML file::

    agent = "claude"
    [[step]]
    screen = "text"         # printed after clearing the terminal
    title = "text"          # optional terminal title (Codex shows a spinner there while working)
    wait = "prompt"         # "prompt" (one line on stdin), a number of seconds, or "forever"

Screens may use ``{reset_clock}`` (local time 30 minutes from now, e.g. ``3:45pm``)
and ``{prompt}`` (the last line received).
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
import tomllib
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from typing import TextIO

CLEAR = "\x1b[2J\x1b[H"


class ScriptError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Step:
    screen: str
    wait: str | float
    title: str | None = None


@dataclass(frozen=True, slots=True)
class Script:
    agent: str
    steps: tuple[Step, ...]


def load_script(path: Path) -> Script:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    agent = data.get("agent")
    if not isinstance(agent, str) or not agent:
        msg = f"{path}: 'agent' must be a non-empty string"
        raise ScriptError(msg)
    steps = []
    for index, raw in enumerate(data.get("step", []), start=1):
        wait = raw.get("wait", 0)
        if not (wait in {"prompt", "forever"} or isinstance(wait, int | float)):
            msg = f"{path}: step {index}: wait must be 'prompt', 'forever' or seconds"
            raise ScriptError(msg)
        title = raw.get("title")
        steps.append(
            Step(
                screen=str(raw.get("screen", "")),
                wait=wait,
                title=str(title) if title is not None else None,
            )
        )
    if not steps:
        msg = f"{path}: no [[step]] entries"
        raise ScriptError(msg)
    return Script(agent=agent, steps=tuple(steps))


def render(screen: str, *, now: datetime, prompt: str) -> str:
    reset = now + timedelta(minutes=30)
    clock = reset.strftime("%I:%M%p").lstrip("0").lower()
    return screen.replace("{reset_clock}", clock).replace("{prompt}", prompt)


class Reporter(Protocol):
    def session(self, session_id: str) -> None: ...


@dataclass
class HerdrReporter:
    """Reports the session id through the herdr CLI, like the agent's herdr hook."""

    binary: str
    pane_id: str
    agent: str

    def session(self, session_id: str) -> None:
        subprocess.run(
            [
                self.binary,
                "pane",
                "report-agent-session",
                "--source",
                f"herdr:{self.agent}",
                "--agent",
                self.agent,
                "--agent-session-id",
                session_id,
                self.pane_id,
            ],
            check=False,
            capture_output=True,
            timeout=10,
        )


class SilentReporter:
    """Outside herdr there is nobody to report to."""

    def session(self, session_id: str) -> None:
        del session_id


def reporter_for(agent: str, env: dict[str, str]) -> Reporter:
    pane_id = env.get("HERDR_PANE_ID")
    binary = env.get("HERDR_BIN_PATH") or shutil.which("herdr")
    if pane_id and binary:
        return HerdrReporter(binary=binary, pane_id=pane_id, agent=agent)
    return SilentReporter()


@dataclass(frozen=True, slots=True)
class Terminal:
    """Where the fake reads prompts and draws screens, and how it tells time."""

    stdin: TextIO
    stdout: TextIO
    now: Callable[[], datetime]
    sleep: Callable[[float], None]


def play(script: Script, *, session_id: str, reporter: Reporter, terminal: Terminal) -> None:
    """Run the steps. Returns when the script ends, stdin closes, or ``/exit`` is typed."""
    reporter.session(session_id)
    prompt = ""
    for step in script.steps:
        title = "" if step.title is None else f"\x1b]0;{step.title}\x07"  # OSC 0: window title
        screen = render(step.screen, now=terminal.now(), prompt=prompt)
        terminal.stdout.write(CLEAR + title + screen)
        terminal.stdout.flush()
        if step.wait == "forever":
            for line in terminal.stdin:
                if line.strip() == "/exit":
                    return
            return
        if step.wait == "prompt":
            line = terminal.stdin.readline()
            if not line or line.strip() == "/exit":
                return
            prompt = line.strip()
        elif isinstance(step.wait, int | float) and step.wait > 0:
            terminal.sleep(float(step.wait))


def make_launcher(directory: Path, agent: str) -> Path:
    """Write ``directory/agent``, an executable that runs this file under that name.

    The interpreter is named by absolute path in the shebang so that the process
    is called ``agent``; through ``/usr/bin/env`` it would be called ``python3``.
    """
    directory.mkdir(parents=True, exist_ok=True)
    launcher = directory / agent
    launcher.write_text(
        f"#!{sys.executable}\n"
        "import runpy, sys\n"
        f"sys.argv[0] = {agent!r}\n"
        f"runpy.run_path({str(Path(__file__).resolve())!r}, run_name='__main__')\n",
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    return launcher


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fake_agent.py", description=__doc__.split("\n\n")[0])
    parser.add_argument("script", type=Path, nargs="?")
    parser.add_argument("--session-id", default=None, help="default: a random UUID")
    parser.add_argument(
        "--make-launcher", nargs=2, metavar=("DIR", "AGENT"), help="write DIR/AGENT and exit"
    )
    args = parser.parse_args(argv)
    if args.make_launcher:
        sys.stdout.write(f"{make_launcher(Path(args.make_launcher[0]), args.make_launcher[1])}\n")
        return 0
    if args.script is None:
        parser.error("a script is required")
    try:
        script = load_script(args.script)
    except (OSError, ScriptError, tomllib.TOMLDecodeError) as err:
        sys.stderr.write(f"{err}\n")
        return 2
    play(
        script,
        session_id=args.session_id or str(uuid.uuid4()),
        reporter=reporter_for(script.agent, dict(os.environ)),
        terminal=Terminal(
            stdin=sys.stdin,
            stdout=sys.stdout,
            now=lambda: datetime.now().astimezone(),
            sleep=time.sleep,
        ),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
