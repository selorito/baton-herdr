"""baton-detect against the usage contract (ADR 0011).

There is no Python usage reader to compare with, so this checks the Rust binary from
the outside: run on the captured samples, every line it writes must validate against
the contract models (and so against schemas/usage-event.v1.json), and its output must
equal the golden files the Rust tests use.

Needs the binary: BATON_DETECT_BIN, else target/debug/baton-detect, else PATH. Skipped
without one, unless BATON_REQUIRE_DETECT is set (CI sets it).
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path

from baton_herdr.core.usage import USAGE_EVENT

from detector_binary import detector

REPO = Path(__file__).parents[2]
GOLDEN = REPO / "crates" / "baton-detect" / "tests" / "golden"
SAMPLES = REPO / "fixtures"

# The columns baton-detect reads, as OpenCode 1.18 creates them.
OPENCODE_SCHEMA = """
CREATE TABLE session (
    id TEXT PRIMARY KEY,
    tokens_input INTEGER NOT NULL DEFAULT 0, tokens_output INTEGER NOT NULL DEFAULT 0,
    tokens_reasoning INTEGER NOT NULL DEFAULT 0, tokens_cache_read INTEGER NOT NULL DEFAULT 0,
    tokens_cache_write INTEGER NOT NULL DEFAULT 0);
CREATE TABLE message (
    id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
    time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL, data TEXT NOT NULL);
"""


def opencode_db(path: Path) -> None:
    """The sample's rows in a database; messages get m1, m2, ... like the Rust test."""
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript(OPENCODE_SCHEMA)
        number = 0
        for line in (SAMPLES / "opencode" / "usage-sample.jsonl").read_text().splitlines():
            record = json.loads(line)
            if record["type"] == "sqlite:session":
                row = record["row"]
                conn.execute(
                    "INSERT INTO session VALUES (?, ?, ?, ?, ?, ?)",
                    [row["id"]]
                    + [row[f"tokens_{k}"] for k in ("input", "output", "reasoning")]
                    + [row["tokens_cache_read"], row["tokens_cache_write"]],
                )
            elif record["type"] == "sqlite:message":
                number += 1
                data = record["data"]
                updated = data["time"].get("completed", data["time"]["created"])
                conn.execute(
                    "INSERT INTO message VALUES (?, '<redacted>', ?, ?, ?)",
                    (f"m{number}", updated, updated, json.dumps(data)),
                )
        conn.commit()


def test_the_binary_writes_the_contract_and_the_golden_files(tmp_path: Path) -> None:
    binary = detector()
    claude = tmp_path / "claude" / "projects" / "-sample"
    codex = tmp_path / "codex" / "sessions" / "2026" / "09" / "30"
    claude.mkdir(parents=True)
    codex.mkdir(parents=True)
    shutil.copy(SAMPLES / "claude" / "usage-sample.jsonl", claude / "sample.jsonl")
    shutil.copy(SAMPLES / "codex" / "usage-sample.jsonl", codex / "rollout-sample.jsonl")
    opencode_db(tmp_path / "opencode.db")

    result = subprocess.run(  # noqa: S603 - the binary under test, fixed arguments
        [
            str(binary),
            "usage",
            "--once",
            "--claude-dir",
            str(tmp_path / "claude" / "projects"),
            "--codex-dir",
            str(tmp_path / "codex" / "sessions"),
            "--opencode-db",
            str(tmp_path / "opencode.db"),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )

    lines = result.stdout.splitlines()
    events = [USAGE_EVENT.validate_json(line) for line in lines]  # the contract
    assert len(events) == len(lines) > 0

    def by_agent(texts: list[str]) -> dict[str, list[object]]:
        grouped: dict[str, list[object]] = {}
        for text in texts:
            value = json.loads(text)
            grouped.setdefault(value["agent"], []).append(value)
        return grouped

    actual = by_agent(lines)
    for agent in ("claude", "codex", "opencode"):
        golden = (GOLDEN / f"{agent}.ndjson").read_text().splitlines()
        assert actual.get(agent) == by_agent(golden)[agent], agent
