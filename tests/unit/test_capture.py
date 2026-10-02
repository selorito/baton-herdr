from __future__ import annotations

import io
import json
import sqlite3
import subprocess
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
        ("cd /home/alice/dev/baton-sandbox", "cd ~/dev/baton-sandbox"),
        ("alice@alice-laptop:~/dev/baton-sandbox$", "<user>@<host>:~/dev/baton-sandbox$"),
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
    value = {"cwd": "/home/alice/dev/baton-sandbox", "token": "abcdefghijk", "input_tokens": 5}
    masked = json.loads(MASKER.mask_json(value))
    assert masked == {"cwd": "~/dev/baton-sandbox", "token": "<redacted>", "input_tokens": 5}


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
    clean = "cwd ~/dev/baton-sandbox\nsource ~/.local/state/herdr/x.toml\n<user>@<host>"
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


def test_record_events_writes_masked_events_and_raises_on_events_lost(
    capsys: pytest.CaptureFixture[str],
) -> None:
    lines = [
        '{"id":"x","result":{"type":"subscription_started"}}',
        '{"event":"pane.agent_status_changed","data":{"pane_id":"w1:p1",'
        '"agent_status":"working","title":"alice@alice-laptop: ~/dev/baton-sandbox"}}',
        '{"id":"x","error":{"code":"events_lost","message":"lagged"}}',
    ]
    sink = io.StringIO()
    with pytest.raises(capture.HerdrError, match="events_lost"):
        capture.record_events(io.StringIO("\n".join(lines)), sink, MASKER, max_events=None)

    written = [json.loads(line) for line in sink.getvalue().splitlines()]
    assert len(written) == 1
    assert written[0]["message"]["data"]["title"] == "<user>@<host>: ~/dev/baton-sandbox"
    # Each recorded event is echoed so the operator can see the recording is alive.
    assert "   1 pane.agent_status_changed -> working" in capsys.readouterr().err


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


def test_unwrap_herdr_output_handles_envelopes_and_bare_objects() -> None:
    assert capture.unwrap_herdr_output({"id": "x", "result": {"pane": 1}}) == {"pane": 1}
    # agent explain --json prints the explanation without an envelope.
    bare = {"agent": "claude", "state": "idle", "matched_rule": None}
    assert capture.unwrap_herdr_output(bare) == bare
    with pytest.raises(capture.HerdrError, match="agent_not_found"):
        capture.unwrap_herdr_output({"id": "x", "error": {"code": "agent_not_found"}})


def test_masker_redacts_oauth_query_parameters() -> None:
    url = (
        "https://claude.ai/oauth/authorize?client_id=abc&response_type=code"
        "&code_challenge=Xy9_Qw-1&code_challenge_method=S256&state=s7aTe&code=c0de42"
    )
    assert MASKER.mask(url) == (
        "https://claude.ai/oauth/authorize?client_id=abc&response_type=code"
        "&code_challenge=<redacted>&code_challenge_method=S256&state=<redacted>&code=<redacted>"
    )
    assert audit_text(Path("u.txt"), MASKER.mask(url), MASKER) == []
    assert {f.kind for f in audit_text(Path("u.txt"), url, MASKER)} == {"oauth_query_param"}


class FakeHerdr(capture.Herdr):
    """Returns canned CLI outputs keyed by the first two arguments."""

    def __init__(self, outputs: dict[tuple[str, str], tuple[int, str, str]]) -> None:
        super().__init__()
        object.__setattr__(self, "outputs", outputs)

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        code, out, err = self.outputs[(args[0], args[1])]  # type: ignore[attr-defined]
        return subprocess.CompletedProcess(list(args), code, out, err)


ERROR = '{"id":"cli:x","error":{"code":"pane_not_found","message":"pane w9:p9 not found"}}'


def test_call_returns_result_bare_object_or_raises_herdr_error() -> None:
    herdr = FakeHerdr(
        {
            ("pane", "get"): (0, '{"id":"cli:pane:get","result":{"pane":{"pane_id":"w1:p1"}}}', ""),
            ("agent", "explain"): (0, '{"agent":"claude","state":"idle"}', ""),
            ("pane", "list"): (1, "", ERROR),
        }
    )
    assert herdr.pane("w1:p1") == {"pane_id": "w1:p1"}
    assert herdr.call("agent", "explain", "w1:p1", "--json") == {"agent": "claude", "state": "idle"}
    with pytest.raises(capture.HerdrError) as caught:
        herdr.call("pane", "list")
    assert (caught.value.code, caught.value.message) == ("pane_not_found", "pane w9:p9 not found")


def test_text_raises_herdr_error_from_stderr_envelope() -> None:
    herdr = FakeHerdr({("pane", "read"): (1, "", ERROR)})
    with pytest.raises(capture.HerdrError, match="pane_not_found"):
        herdr.text("pane", "read", "w9:p9")


def test_explain_failure_is_recorded_not_raised() -> None:
    herdr = FakeHerdr(
        {("agent", "explain"): (1, "", ERROR.replace("pane_not_found", "agent_not_found"))}
    )
    explain, reason = capture.explain_pane(herdr, "w1:p1")
    assert explain == {"error": {"code": "agent_not_found", "message": "pane w9:p9 not found"}}
    assert reason == "agent explain failed: agent_not_found"


def test_snap_stops_with_a_clear_message_when_the_pane_is_missing() -> None:
    herdr = FakeHerdr({("pane", "get"): (1, "", ERROR)})
    args = capture.build_parser().parse_args(
        ["snap", "--pane", "w9:p9", "--agent", "claude", "--scenario", "idle"]
    )
    with pytest.raises(capture.CaptureError, match=r"cannot read pane w9:p9: .*pane_not_found"):
        capture.snap(args, herdr, MASKER)


def test_names_right_after_a_terminal_escape_are_masked_and_audited() -> None:
    # A coloured shell prompt in screen.ansi: the escape ends in "m", a letter.
    prompt = "\x1b[1m\x1b[38;5;2malice@alice-laptop:~/dev/baton-sandbox$ "
    assert MASKER.mask(prompt) == "\x1b[1m\x1b[38;5;2m<user>@<host>:~/dev/baton-sandbox$ "
    findings = audit_text(Path("screen.ansi"), prompt, MASKER)
    assert {finding.kind for finding in findings} >= {"username", "hostname"}


@pytest.mark.parametrize(
    ("line", "flagged"),
    [
        ("cd ~/dev/baton-sandbox/src", False),
        ("user@<host>:~/dev/old-sandbox$ ", False),  # a capture from before a rename
        ('"cwd": "~/dev/baton-sandbox\\n"', False),
        ("cat ~/dev/baton-sandboxes/x", True),
        ("open ~/dev/myproject/app.py", True),
    ],
)
def test_any_sandbox_named_by_the_convention_is_allowed(line: str, *, flagged: bool) -> None:
    findings = audit_text(Path("screen.txt"), line, MASKER)
    assert any(f.kind == "path_outside_sandbox" for f in findings) is flagged


def test_record_ids_get_stable_pseudonyms_so_split_records_still_deduplicate() -> None:
    # Claude splits one API response into several records sharing message.id.
    first = {"type": "assistant", "requestId": "req_011CA1", "message": {"id": "msg_01AbCdEf"}}
    second = {"type": "assistant", "requestId": "req_011CA1", "message": {"id": "msg_01AbCdEf"}}
    other = {"type": "assistant", "requestId": "req_011CA2", "message": {"id": "msg_01ZzZzZz"}}
    sanitizer = Sanitizer(key=b"k")
    a, b, c = (sanitizer.clean(r) for r in (first, second, other))

    assert a["message"]["id"] == b["message"]["id"] != c["message"]["id"]
    assert a["requestId"] == b["requestId"] != c["requestId"]
    assert a["message"]["id"].startswith("msg_")
    assert a["requestId"].startswith("req_")
    assert "01AbCdEf" not in json.dumps(a)
    # Deterministic for a key; another key gives other pseudonyms.
    assert Sanitizer(key=b"k").clean(first) == a
    assert Sanitizer(key=b"other").clean(first)["message"]["id"] != a["message"]["id"]


def test_free_text_under_an_id_key_is_still_redacted() -> None:
    assert Sanitizer().clean({"id": "not an id, a sentence"}) == {"id": "<redacted>"}
