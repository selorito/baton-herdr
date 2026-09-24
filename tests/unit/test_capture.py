from __future__ import annotations

import io
import json
import sqlite3
from pathlib import Path

import pytest

import capture
from capture import Masker, Sanitizer, audit_text, outside_sandbox, with_default_subcommand

MASKER = Masker(home="/home/alice", user="alice", host="alice-laptop")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("key sk-ant-api03-AbCdEf0123456789xyz end", "key <anthropic_key> end"),
        ("OPENAI=sk-proj-abcdefghijklmnopqrstuvwx", "OPENAI=<openai_key>"),
        ("g AIzaSyA1234567890abcdefghijklmnopqrstuv", "g <google_api_key>"),
        ("gh ghp_" + "a" * 36, "gh <github_token>"),
        ("aws AKIAABCDEFGHIJKLMNOP", "aws <aws_access_key>"),
        ("Authorization: Bearer abcdef0123456789abcdef", "Authorization: <bearer>"),
        ("jwt eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.SflKxwRJSMeKKF2QT4", "jwt <jwt>"),
        ('"api_key": "hunter2hunter2"', '"api_key": "<redacted>"'),
        ("GITHUB_TOKEN=abc123def456ghi", "GITHUB_TOKEN=<redacted>"),
        ("mail bob.smith@example.org now", "mail <email> now"),
        ("cd /home/alice/dev/coban-sandbox", "cd ~/dev/coban-sandbox"),
        ("alice@alice-laptop:~/dev/coban-sandbox$", "<user>@<host>:~/dev/coban-sandbox$"),
        ("see /home/bob/secret-project", "see /home/<user>/secret-project"),
        ("dir -home-alice-Desktop-work", "dir -home-<user>-Desktop-work"),
    ],
)
def test_masker_removes_secrets_and_identity(raw: str, expected: str) -> None:
    assert MASKER.mask(raw) == expected


@pytest.mark.parametrize(
    "text",
    [
        '"input_tokens": 123456789,',
        "task-1234 finished in 12s",
        "tokens used: 12.3k",
        "/home/alicexyz is another user",  # does not match the home dir prefix
        "the calculator returns 3",
    ],
)
def test_masker_leaves_ordinary_text_alone(text: str) -> None:
    if text.startswith("/home/alicexyz"):
        assert MASKER.mask(text) == "/home/<user> is another user"
    else:
        assert MASKER.mask(text) == text


def test_mask_json_keeps_json_valid() -> None:
    value = {"cwd": "/home/alice/dev/coban-sandbox", "token": "abcdefghijk", "input_tokens": 5}
    masked = json.loads(MASKER.mask_json(value))
    assert masked == {"cwd": "~/dev/coban-sandbox", "token": "<redacted>", "input_tokens": 5}


def test_sanitizer_keeps_structure_and_usage_but_no_content() -> None:
    record = {
        "type": "assistant",
        "cwd": "/home/alice/work/secret",
        "gitBranch": "feature/secret",
        "sessionId": "3f2c1a9e-1111-4222-8333-444455556666",
        "message": {
            "model": "claude-example-1",
            "role": "assistant",
            "content": [{"type": "text", "text": "here is the private code"}],
            "usage": {"input_tokens": 12, "output_tokens": 34, "cache_read_input_tokens": 0},
        },
        "snapshot": {"/home/alice/work/secret/app.py": {"v": 1}},
    }
    clean = Sanitizer().clean(record)
    dumped = json.dumps(clean)

    assert clean["type"] == "assistant"
    assert clean["cwd"] == clean["gitBranch"] == "<redacted>"
    assert clean["sessionId"] == "00000000-0000-4000-8000-000000000001"
    assert clean["message"]["model"] == "claude-example-1"
    assert clean["message"]["usage"] == record["message"]["usage"]  # type: ignore[index]
    assert clean["message"]["content"] == [{"type": "text", "text": "<redacted>"}]
    assert "secret" not in dumped
    assert "private code" not in dumped


def test_sanitizer_maps_the_same_uuid_to_the_same_fake() -> None:
    sanitizer = Sanitizer()
    real = "3f2c1a9e-1111-4222-8333-444455556666"
    assert sanitizer.clean({"a": real, "b": [real]}) == {
        "a": "00000000-0000-4000-8000-000000000001",
        "b": ["00000000-0000-4000-8000-000000000001"],
    }


@pytest.mark.parametrize(
    ("pane", "problems"),
    [
        ({"cwd": "/sb", "foreground_cwd": "/sb/sub"}, []),
        ({"cwd": "/sb/sub", "foreground_cwd": None}, []),
        ({"cwd": "/elsewhere", "foreground_cwd": "/sb"}, ["cwd=/elsewhere"]),
        ({"cwd": "/sb", "foreground_cwd": "/sb-other"}, ["foreground_cwd=/sb-other"]),
        ({"cwd": None}, ["cwd=<unknown>"]),
    ],
)
def test_outside_sandbox(pane: dict[str, str | None], problems: list[str]) -> None:
    assert outside_sandbox(pane, Path("/sb")) == problems


def test_audit_flags_leaks_and_accepts_masked_sandbox_text() -> None:
    clean = "cwd ~/dev/coban-sandbox\nsource ~/.local/state/herdr/x.toml\n<user>@<host>"
    assert audit_text(Path("ok.txt"), clean, MASKER) == []

    leaky = "\n".join(
        [
            "open ~/Desktop/private/notes.md",
            "user alice logged in",
            "path /home/bob/x",
            "mail bob@example.org",
            "key sk-ant-api03-AbCdEf0123456789xyz",
            "project -home-<user>-Desktop-client",
        ]
    )
    kinds = {f.kind for f in audit_text(Path("bad.txt"), leaky, MASKER)}
    assert kinds == {
        "path_outside_sandbox",
        "username",
        "absolute_home_path",
        "email",
        "anthropic_key",
        "encoded_project_path",
    }


def test_record_events_writes_masked_events_and_raises_on_events_lost() -> None:
    lines = [
        '{"id":"x","result":{"type":"subscription_started"}}',
        '{"event":"pane.agent_status_changed","data":{"pane_id":"w1:p1",'
        '"agent_status":"working","title":"alice@alice-laptop: ~/dev/coban-sandbox"}}',
        '{"id":"x","error":{"code":"events_lost","message":"lagged"}}',
    ]
    sink = io.StringIO()
    with pytest.raises(capture.HerdrApiError, match="events_lost"):
        capture.record_events(io.StringIO("\n".join(lines)), sink, MASKER, max_events=None)

    written = [json.loads(line) for line in sink.getvalue().splitlines()]
    assert len(written) == 1
    assert written[0]["message"]["data"]["title"] == "<user>@<host>: ~/dev/coban-sandbox"


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["--pane", "w1:p1"], ["snap", "--pane", "w1:p1"]),
        (["watch", "--pane", "w1:p1"], ["watch", "--pane", "w1:p1"]),
        (["--herdr-session", "t", "--pane", "p"], ["--herdr-session", "t", "snap", "--pane", "p"]),
    ],
)
def test_snap_is_the_default_subcommand(argv: list[str], expected: list[str]) -> None:
    assert with_default_subcommand(argv) == expected


def test_socket_path_follows_herdr_lookup() -> None:
    home = Path("/h")
    assert capture.resolve_socket_path({"HERDR_SOCKET_PATH": "/s"}, home, None) == Path("/s")
    assert capture.resolve_socket_path({"HERDR_SOCKET_PATH": "/s"}, home, "t") == Path(
        "/h/.config/herdr/sessions/t/herdr.sock"
    )
    assert capture.resolve_socket_path({}, home, None) == Path("/h/.config/herdr/herdr.sock")


def test_iter_jsonl_tolerates_control_characters_and_broken_lines(tmp_path: Path) -> None:
    log = tmp_path / "log.jsonl"
    log.write_text('{"type": "a", "text": "x\ty"}\n{not json\n\n[1]\n{"type": "b"}\n')

    assert [r["type"] for r in capture.iter_jsonl(log)] == ["a", "b"]


def test_iter_opencode_db_reads_sessions_and_messages(tmp_path: Path) -> None:
    db = tmp_path / "opencode.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE session (id TEXT, time_created INT, tokens_input INT)")
        conn.execute("CREATE TABLE message (data TEXT, time_created INT)")
        conn.execute("INSERT INTO session VALUES ('s1', 1, 42)")
        conn.execute(
            """INSERT INTO message VALUES ('{"role": "assistant", "tokens": {"input": 7}}', 1)"""
        )

    records = list(capture.iter_opencode_db(db))

    assert records == [
        {"type": "sqlite:session", "row": {"id": "s1", "time_created": 1, "tokens_input": 42}},
        {"type": "sqlite:message", "data": {"role": "assistant", "tokens": {"input": 7}}},
    ]
