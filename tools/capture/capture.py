#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Record coding-agent screens and herdr events as baton fixtures.

Standalone on purpose: stdlib plus the ``herdr`` CLI and socket only. It is not
part of the ``baton`` package.

Subcommands (``snap`` is the default when no subcommand is given)::

    capture.py [snap] --pane w1:p2 --agent claude --scenario rate_limited [--lines 200]
    capture.py watch --pane w1:p2 [--agent claude]
    capture.py herdr-ref
    capture.py usage-sample --agent claude --input LOG.jsonl
    capture.py audit [fixtures]

Everything written under ``fixtures/`` goes through :class:`Masker` first, and
``audit`` re-checks the whole tree before commit.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import hmac
import json
import os
import re
import secrets
import shlex
import socket
import sqlite3
import subprocess
import sys
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_DIR = REPO_ROOT / "fixtures"
DEFAULT_SANDBOX = Path("~/dev/baton-sandbox")
# Sandboxes are directories named "<name>-sandbox" directly under ~/dev. The pattern,
# not one name, is allowed: captures recorded before the project was renamed
# (ADR 0010) keep the sandbox name they were made in.
_SANDBOX_DIR = r"dev/[a-z0-9]+-sandbox"
_SANDBOX_TILDE = re.compile(rf"~/{_SANDBOX_DIR}(?![A-Za-z0-9_.-])")
HERDR_BIN = os.environ.get("HERDR_BIN", "herdr")
COMMAND_TIMEOUT_S = 30


class Scenario(StrEnum):
    IDLE = "idle"
    WORKING = "working"
    BLOCKED_PERMISSION = "blocked_permission"
    BLOCKED_QUESTION = "blocked_question"
    DONE = "done"
    RATE_LIMITED = "rate_limited"
    CONTEXT_FULL = "context_full"
    CRASHED = "crashed"
    RESUME_PROMPT = "resume_prompt"


class Agent(StrEnum):
    CLAUDE = "claude"
    CODEX = "codex"
    GEMINI = "gemini"
    OPENCODE = "opencode"


class CaptureError(Exception):
    """A capture step failed; the message is shown to the user."""


# --------------------------------------------------------------------------- masking

REDACTED = "<redacted>"

_SECRET_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "private_key",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    ),
    ("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9_-]{16,}")),
    ("openai_key", re.compile(r"\bsk-(?!ant-)(?:proj-|svcacct-|admin-)?[A-Za-z0-9_-]{20,}")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}")),
    ("google_oauth_token", re.compile(r"\bya29\.[0-9A-Za-z_-]{20,}")),
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{22,})")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}")),
)
# key=value / "key": "value" where the key names a secret. Pure numbers are
# kept so usage fields such as "input_tokens": 12345678 survive.
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b([A-Za-z0-9_-]*(?:api[_-]?key|access[_-]?key|token|secret|passwd|password)"
    r"[A-Za-z0-9_-]*)"
    r"([\"']?\s*[:=]\s*[\"']?)"
    r"(?![0-9]+(?:[\"',\s}]|$))(?!<)([^\s\"',;}]{8,})"
)
# OAuth values in URLs, e.g. the login links agents print on first run.
_OAUTH_QUERY = re.compile(
    r"(?i)([?&](?:code|state|code_challenge|code_verifier|access_token|id_token|refresh_token)=)"
    r"(?!<)[^&\s\"'#<>]+"
)
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_OTHER_HOME = re.compile(r"(/home/|/Users/)(?!<user>)[^/\s\"'<>:]+")
_ROOT_HOME = re.compile(r"(?<![\w.~-])/root(?=/|\b)")


def _word(literal: str) -> re.Pattern[str]:
    # Alphanumeric boundaries only, so "-home-alice-dev" (encoded paths) masks too.
    # A terminal escape right before the name ("\x1b[32malice@host", a coloured
    # prompt in screen.ansi) ends in a letter, so it counts as a boundary too; it
    # is kept in the "csi" group.
    return re.compile(
        rf"(?:(?P<csi>\x1b\[[0-9;:?]*[@-~])|(?<![A-Za-z0-9])){re.escape(literal)}(?![A-Za-z0-9])"
    )


def _replace_word(placeholder: str) -> Callable[[re.Match[str]], str]:
    return lambda match: (match.group("csi") or "") + placeholder


@dataclass(frozen=True)
class Masker:
    """Removes secrets and machine identity from captured text."""

    home: str
    user: str
    host: str
    _identity: tuple[tuple[re.Pattern[str], str], ...] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        rules: list[tuple[re.Pattern[str], str]] = []
        home = self.home.rstrip("/")
        if home:
            rules.append((re.compile(rf"{re.escape(home)}(?![A-Za-z0-9_.-])"), "~"))
        for literal, placeholder in ((self.host, "<host>"), (self.user, "<user>")):
            # Shorter names would collide with ordinary words.
            if len(literal) >= 3:
                rules.append((_word(literal), placeholder))
        object.__setattr__(self, "_identity", tuple(rules))

    @classmethod
    def for_this_machine(cls) -> Masker:
        return cls(home=str(Path.home()), user=getpass.getuser(), host=socket.gethostname())

    def mask(self, text: str) -> str:
        for name, pattern in _SECRET_RULES:
            text = pattern.sub(f"<{name}>", text)
        text = _OAUTH_QUERY.sub(lambda m: f"{m.group(1)}{REDACTED}", text)
        text = _SECRET_ASSIGNMENT.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)
        text = _EMAIL.sub("<email>", text)
        for pattern, placeholder in self._identity:
            text = pattern.sub(
                _replace_word(placeholder) if "csi" in pattern.groupindex else placeholder, text
            )
        text = _OTHER_HOME.sub(r"\1<user>", text)
        return _ROOT_HOME.sub("/<root>", text)

    def mask_json(self, value: Any) -> str:
        """Serialize, mask, and check the result is still valid JSON."""
        masked = self.mask(json.dumps(value, indent=2, ensure_ascii=False)) + "\n"
        try:
            json.loads(masked)
        except json.JSONDecodeError as err:
            msg = f"masking produced invalid JSON: {err}"
            raise CaptureError(msg) from err
        return masked


# --------------------------------------------------------------------------- herdr CLI


@dataclass(frozen=True)
class Herdr:
    """Thin wrapper over the herdr CLI. ``session`` maps to ``herdr --session``."""

    session: str | None = None
    binary: str = HERDR_BIN

    def argv(self, *args: str) -> list[str]:
        base = [self.binary]
        if self.session:
            base += ["--session", self.session]
        return [*base, *args]

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                self.argv(*args),
                capture_output=True,
                text=True,
                timeout=COMMAND_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as err:
            msg = f"`{shlex.join(self.argv(*args))}` could not run: {err}"
            raise CaptureError(msg) from err

    def text(self, *args: str) -> str:
        """Run a plain-text command such as ``pane read``; errors still come as JSON."""
        proc = self._run(*args)
        if proc.returncode != 0:
            error = _error_envelope(proc.stderr)
            if error is not None:
                raise HerdrError(error)
            msg = f"`{shlex.join(self.argv(*args))}` failed: {proc.stderr.strip() or proc.stdout}"
            raise CaptureError(msg)
        return proc.stdout

    def call(self, *args: str) -> dict[str, Any]:
        """Run a ``--json`` command and return its payload (see :func:`unwrap_herdr_output`)."""
        proc = self._run(*args)
        # herdr prints success on stdout and the error envelope on stderr.
        try:
            output = json.loads(proc.stdout or proc.stderr)
        except json.JSONDecodeError as err:
            msg = f"`{shlex.join(self.argv(*args))}` returned no JSON: {proc.stderr.strip()}"
            raise CaptureError(msg) from err
        if not isinstance(output, dict):
            msg = f"`{shlex.join(self.argv(*args))}` returned JSON that is not an object"
            raise CaptureError(msg)
        return unwrap_herdr_output(output)

    def version(self) -> str:
        return self.text("--version").strip().removeprefix("herdr ").strip()

    def pane(self, pane_id: str) -> dict[str, Any]:
        pane: dict[str, Any] = self.call("pane", "get", pane_id)["pane"]
        return pane


def unwrap_herdr_output(output: dict[str, Any]) -> dict[str, Any]:
    """Return the payload of a herdr 0.9.1 CLI JSON response.

    Verified shapes: ``{"id", "result"}`` from ``pane get``, ``pane list`` and
    ``server agent-manifests --json``; a bare object from ``agent explain --json``
    and ``api schema --json``; ``{"id", "error": {"code", "message"}}`` on stderr
    (exit 1) for every failing command, including ``pane read``.
    """
    if "id" in output and isinstance(output.get("error"), dict):
        raise HerdrError(output["error"])
    if "id" in output and "result" in output:
        result: dict[str, Any] = output["result"]
        return result
    return output


def _error_envelope(stderr: str) -> dict[str, Any] | None:
    try:
        output = json.loads(stderr)
    except json.JSONDecodeError:
        return None
    if isinstance(output, dict) and isinstance(output.get("error"), dict):
        error: dict[str, Any] = output["error"]
        return error
    return None


class HerdrError(CaptureError):
    """herdr answered with an error envelope."""

    def __init__(self, error: dict[str, Any]) -> None:
        self.error = error
        self.code: str | None = error.get("code")
        self.message: str | None = error.get("message")
        super().__init__(f"herdr error {self.code}: {self.message}")


# --------------------------------------------------------------------------- sandbox


def outside_sandbox(pane: dict[str, Any], sandbox: Path) -> list[str]:
    """Return the pane directories that are not inside ``sandbox`` (unknown counts)."""
    root = sandbox.expanduser().resolve()
    problems = []
    for key in ("cwd", "foreground_cwd"):
        value = pane.get(key)
        if value is None:
            if key == "cwd":
                problems.append("cwd=<unknown>")
            continue
        path = Path(value).expanduser().resolve()
        if path != root and root not in path.parents:
            problems.append(f"{key}={value}")
    return problems


def check_sandbox(pane: dict[str, Any], sandbox: Path, *, allow_outside: bool) -> None:
    problems = outside_sandbox(pane, sandbox)
    if not problems:
        return
    warning = f"warning: pane is outside the sandbox {sandbox}: {', '.join(problems)}"
    if not allow_outside:
        msg = f"{warning}\nrefusing to save; pass --allow-outside-sandbox to override"
        raise CaptureError(msg)
    sys.stderr.write(warning + "\n")


# --------------------------------------------------------------------------- snap


def utc_now() -> datetime:
    return datetime.now(UTC)


def stamp(moment: datetime) -> str:
    return moment.strftime("%Y%m%dT%H%M%SZ")


def agent_version(agent: Agent) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            [agent.value, "--version"],
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as err:
        return {"version": None, "error": str(err)}
    lines = (proc.stdout or proc.stderr).strip().splitlines()
    if proc.returncode != 0 or not lines:
        return {"version": None, "error": f"exit {proc.returncode}"}
    return {"version": lines[-1].strip(), "error": None}


def snap(args: argparse.Namespace, herdr: Herdr, masker: Masker) -> Path:
    try:
        pane = herdr.pane(args.pane)
    except CaptureError as err:
        msg = f"cannot read pane {args.pane}: {err}"
        raise CaptureError(msg) from err
    check_sandbox(pane, args.sandbox, allow_outside=args.allow_outside_sandbox)

    lines = str(args.lines)
    try:
        screen = herdr.text("pane", "read", args.pane, "--source", "recent", "--lines", lines)
        screen_ansi = herdr.text(
            "pane", "read", args.pane, "--source", "recent", "--lines", lines, "--format", "ansi"
        )
        detection = herdr.text("pane", "read", args.pane, "--source", "detection")
    except CaptureError as err:
        msg = f"cannot read the screen of pane {args.pane}: {err}"
        raise CaptureError(msg) from err
    # explain is best effort: a screen herdr cannot classify is still worth keeping.
    explain, explain_error = explain_pane(herdr, args.pane)

    moment = utc_now()
    scroll = pane.get("scroll") or {}
    meta = {
        "schema": 1,
        "agent": args.agent.value,
        "agent_cli": agent_version(args.agent),
        "scenario": args.scenario.value,
        "captured_at": moment.isoformat(),
        "herdr_version": herdr.version(),
        "pane_id": args.pane,
        "herdr_agent": pane.get("agent"),
        "herdr_agent_status": pane.get("agent_status"),
        "terminal": {
            "rows": scroll.get("viewport_rows"),
            # herdr 0.9.1 PaneInfo exposes viewport_rows but no column count.
            "cols": None,
        },
        "recent_lines": args.lines,
        "note": args.note,
        "explain_error": explain_error,
    }

    out: Path = FIXTURES_DIR / args.agent.value / args.scenario.value / stamp(moment)
    files = {
        "screen.txt": masker.mask(screen),
        "screen.ansi": masker.mask(screen_ansi),
        "screen.detection.txt": masker.mask(detection),
        "explain.json": masker.mask_json(explain),
        "pane.json": masker.mask_json(pane),
        "meta.json": masker.mask_json(meta),
    }
    out.mkdir(parents=True, exist_ok=False)
    for name, content in files.items():
        (out / name).write_text(content, encoding="utf-8")
    return out


def explain_pane(herdr: Herdr, pane_id: str) -> tuple[dict[str, Any], str | None]:
    """Return herdr's explanation, or an ``{"error": ...}`` stand-in and a short reason."""
    try:
        return herdr.call("agent", "explain", pane_id, "--json"), None
    except HerdrError as err:
        return {"error": err.error}, f"agent explain failed: {err.code}"
    except CaptureError as err:
        return {"error": {"code": None, "message": str(err)}}, "agent explain failed"


# --------------------------------------------------------------------------- watch


def resolve_socket_path(env: dict[str, str], home: Path, session: str | None) -> Path:
    """herdr's lookup order (see baton_herdr.core.config.resolve_herdr_socket_path)."""
    if not session and (explicit := env.get("HERDR_SOCKET_PATH")):
        return Path(explicit)
    xdg = env.get("XDG_CONFIG_HOME")
    config_dir = (Path(xdg) if xdg else home / ".config") / "herdr"
    name = session or env.get("HERDR_SESSION")
    if name and name != "default":
        return config_dir / "sessions" / name / "herdr.sock"
    return config_dir / "herdr.sock"


def subscribe_request(pane_id: str) -> str:
    request = {
        "id": "baton-capture-watch",
        "method": "events.subscribe",
        "params": {"subscriptions": [{"type": "pane.agent_status_changed", "pane_id": pane_id}]},
    }
    return json.dumps(request) + "\n"


def watch(args: argparse.Namespace, herdr: Herdr, masker: Masker) -> Path:
    pane = herdr.pane(args.pane)
    check_sandbox(pane, args.sandbox, allow_outside=args.allow_outside_sandbox)
    agent = args.agent.value if args.agent else pane.get("agent")
    if agent not in {a.value for a in Agent}:
        msg = f"pane agent is {agent!r}; pass --agent to choose the fixtures directory"
        raise CaptureError(msg)

    socket_path = args.socket or resolve_socket_path(dict(os.environ), Path.home(), herdr.session)
    out: Path = FIXTURES_DIR / agent / "_events" / f"{stamp(utc_now())}.ndjson"
    out.parent.mkdir(parents=True, exist_ok=True)

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.connect(str(socket_path))
        conn.sendall(subscribe_request(args.pane).encode())
        reader = conn.makefile("r", encoding="utf-8")
        with out.open("x", encoding="utf-8") as sink:
            count = record_events(reader, sink, masker, max_events=args.max_events)
    sys.stderr.write(f"recorded {count} event(s)\n")
    return out


def record_events(reader: TextIO, sink: TextIO, masker: Masker, *, max_events: int | None) -> int:
    """Copy subscription events to ``sink`` as masked NDJSON with a receive timestamp.

    Returns the number of events written. Raises on herdr errors such as
    ``events_lost`` so a gap in the recording is never silent.
    """
    count = 0
    try:
        for line in reader:
            if not line.strip():
                continue
            message: dict[str, Any] = json.loads(line)
            if "error" in message:
                raise HerdrError(message["error"])
            if message.get("result", {}).get("type") == "subscription_started":
                sys.stderr.write("subscribed; waiting for events (Ctrl+C to stop)\n")
                continue
            record = {"received_at": utc_now().isoformat(), "message": message}
            sink.write(masker.mask(json.dumps(record, ensure_ascii=False)) + "\n")
            sink.flush()
            count += 1
            # Echo a one-line summary: the file is the record, this shows it is alive.
            data = message.get("data") or {}
            sys.stderr.write(f"{count:>4} {message.get('event')} -> {data.get('agent_status')}\n")
            if max_events is not None and count >= max_events:
                break
    except KeyboardInterrupt:
        pass
    return count


# --------------------------------------------------------------------------- herdr-ref


def herdr_ref(args: argparse.Namespace, herdr: Herdr, masker: Masker) -> Path:
    version = herdr.version()
    out = FIXTURES_DIR / "herdr"
    out.mkdir(parents=True, exist_ok=True)
    schema = json.loads(herdr.text("api", "schema", "--json"))
    manifests = herdr.call("server", "agent-manifests", "--json")
    (out / f"api-schema-{version}.json").write_text(masker.mask_json(schema), encoding="utf-8")
    (out / f"agent-manifests-{version}.json").write_text(
        masker.mask_json(manifests), encoding="utf-8"
    )

    # Copy the active detection rules for the agents baton supports.
    detection_dir = out / "agent-detection"
    detection_dir.mkdir(exist_ok=True)
    wanted = {a.value for a in Agent}
    for entry in manifests.get("manifests", []):
        if entry.get("agent") not in wanted:
            continue
        source = str(entry.get("source", ""))
        path = Path(source.split(":", 1)[1]) if ":" in source else None
        if path is None or not path.is_file():
            sys.stderr.write(f"skipping {entry.get('agent')}: no readable source {source!r}\n")
            continue
        target = detection_dir / f"{entry['agent']}-{entry['active_version']}.toml"
        target.write_text(masker.mask(path.read_text(encoding="utf-8")), encoding="utf-8")
    del args
    return out


# --------------------------------------------------------------------------- usage-sample

# String values kept verbatim. Everything else that is a string is redacted, so
# prompts, code, paths (cwd), branch names and titles never survive.
KEPT_STRING_KEYS = frozenset(
    {"type", "subtype", "role", "model", "stop_reason", "service_tier", "timestamp", "status"}
)
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
# Record identifiers that are not UUIDs: Claude's message.id ("msg_…") and requestId
# ("req_…"), Codex response and call ids, OpenCode message ids. Tools deduplicate by
# them, so they are pseudonymised (same id, same pseudonym), not redacted.
ID_KEYS = frozenset(
    {
        "id",
        "requestId",
        "request_id",
        "response_id",
        "messageId",
        "message_id",
        "messageID",
        "parentID",
        "call_id",
        "tool_use_id",
    }
)
_ID_VALUE = re.compile(r"^(?:(?P<prefix>[A-Za-z]{1,8})_)?[A-Za-z0-9_-]{6,128}$")
_PLAIN_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
MAX_ARRAY_ITEMS = 3


@dataclass
class Sanitizer:
    """Whitelist-based structure-preserving redaction for agent session logs."""

    uuids: dict[str, str] = field(default_factory=dict)
    # Random per run, so a pseudonym cannot be traced back by hashing known ids.
    key: bytes = field(default_factory=lambda: secrets.token_bytes(32))

    def pseudonym(self, real: str) -> str:
        """Same id, same pseudonym, keeping a short type prefix such as ``msg_``."""
        match = _ID_VALUE.match(real)
        prefix = match["prefix"] if match and match["prefix"] else "id"
        digest = hmac.new(self.key, real.encode(), hashlib.sha256).hexdigest()[:24]
        return f"{prefix}_{digest}"

    def fake_uuid(self, real: str) -> str:
        if real not in self.uuids:
            self.uuids[real] = f"00000000-0000-4000-8000-{len(self.uuids) + 1:012d}"
        return self.uuids[real]

    def clean(self, value: Any, key: str | None = None) -> Any:
        if isinstance(value, dict):
            cleaned: dict[str, Any] = {}
            for index, (k, v) in enumerate(value.items()):
                safe_key = k if _PLAIN_KEY.match(k) else f"<key-{index}>"
                cleaned[safe_key] = self.clean(v, k)
            return cleaned
        if isinstance(value, list):
            return [self.clean(item, key) for item in value[:MAX_ARRAY_ITEMS]]
        if isinstance(value, str):
            return self._clean_string(value, key)
        return value  # numbers, booleans, null

    def _clean_string(self, value: str, key: str | None) -> str:
        if key in KEPT_STRING_KEYS:
            return value
        if _UUID.match(value):
            return self.fake_uuid(value)
        if key in ID_KEYS and _ID_VALUE.match(value):
            return self.pseudonym(value)
        return REDACTED


def record_kind(record: dict[str, Any]) -> str:
    payload = record.get("payload")
    inner = payload.get("type") if isinstance(payload, dict) else None
    return f"{record.get('type')}/{inner}" if inner else str(record.get("type"))


_USAGE_MARKERS = ('"usage"', '"token_count"', '"total_token_usage"', '"tokens"', '"tokens_input"')


def has_usage(record: dict[str, Any]) -> bool:
    text = json.dumps(record)
    return any(marker in text for marker in _USAGE_MARKERS)


def iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    """Yield JSON objects, tolerating raw control characters and skipping broken lines.

    Claude Code transcripts have been seen with a raw control character inside
    a string and with a line that does not parse at all.
    """
    with path.open(encoding="utf-8") as source:
        for line in source:
            if not line.strip():
                continue
            try:
                record = json.loads(line, strict=False)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                yield record


def iter_opencode_db(path: Path) -> Iterator[dict[str, Any]]:
    """Yield OpenCode session rows and message ``data`` documents, read-only."""
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        for row in conn.execute("SELECT * FROM session ORDER BY time_created"):
            yield {"type": "sqlite:session", "row": dict(row)}
        for (data,) in conn.execute("SELECT data FROM message ORDER BY time_created"):
            message = json.loads(data, strict=False)
            if isinstance(message, dict):
                yield {"type": "sqlite:message", "data": message}


def usage_sample(args: argparse.Namespace, _herdr: Herdr, masker: Masker) -> Path:
    per_kind: dict[str, int] = {}
    usage_kept = 0
    sanitizer = Sanitizer(key=args.pseudonym_key.encode()) if args.pseudonym_key else Sanitizer()
    out_lines: list[str] = []
    source = Path(args.input).expanduser()
    records = iter_opencode_db(source) if source.suffix == ".db" else iter_jsonl(source)
    for record in records:
        kind = record_kind(record)
        usage = has_usage(record)
        if per_kind.get(kind, 0) >= args.per_kind and not (usage and usage_kept < args.usage):
            continue
        per_kind[kind] = per_kind.get(kind, 0) + 1
        usage_kept += int(usage)
        out_lines.append(json.dumps(sanitizer.clean(record), ensure_ascii=False))
    out = Path(args.out) if args.out else FIXTURES_DIR / args.agent.value / "usage-sample.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(masker.mask("\n".join(out_lines)) + "\n", encoding="utf-8")
    return out


# --------------------------------------------------------------------------- audit

_ALLOWED_TILDE_PREFIXES = (
    "~/.config/herdr",
    "~/.local/state/herdr",
)
_TILDE_PATH = re.compile(r"(?<![\w/])~/[^\s\"'`<>|:,;)\]}]*")
_ABSOLUTE_HOME = re.compile(r"(?:/home/|/Users/|[A-Za-z]:\\Users\\)(?!<user>)[^/\\\s\"'<>]+")
# Claude encodes project paths as directory names, e.g. -home-<user>-dev-foo.
_ENCODED_PROJECT = re.compile(r"-home-<user>-(?!dev-[a-z0-9]+-sandbox\b)[A-Za-z0-9._-]+")
_SKIP_AUDIT = frozenset({".gitkeep"})


@dataclass(frozen=True)
class Finding:
    path: Path
    line: int
    kind: str
    excerpt: str


def audit_text(path: Path, text: str, masker: Masker) -> list[Finding]:
    findings: list[Finding] = []
    identity = [
        (kind, _word(literal))
        for kind, literal in (("username", masker.user), ("hostname", masker.host))
        if len(literal) >= 3
    ]
    for number, line in enumerate(text.splitlines(), start=1):
        checks: list[tuple[str, re.Pattern[str]]] = [
            *_SECRET_RULES,
            ("secret_assignment", _SECRET_ASSIGNMENT),
            ("oauth_query_param", _OAUTH_QUERY),
            ("email", _EMAIL),
            ("absolute_home_path", _ABSOLUTE_HOME),
            ("encoded_project_path", _ENCODED_PROJECT),
            *identity,
        ]
        findings.extend(
            Finding(path, number, kind, match.group(0)[:80])
            for kind, pattern in checks
            for match in pattern.finditer(line)
        )
        for match in _TILDE_PATH.finditer(line):
            candidate = match.group(0)
            if not candidate.startswith(_ALLOWED_TILDE_PREFIXES) and not _SANDBOX_TILDE.match(
                candidate
            ):
                findings.append(Finding(path, number, "path_outside_sandbox", candidate[:80]))
    return findings


def audit(args: argparse.Namespace, _herdr: Herdr, masker: Masker) -> Path:
    root = Path(args.root)
    findings: list[Finding] = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        if path.name in _SKIP_AUDIT:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            findings.append(Finding(path, 0, "binary_file", "not UTF-8 text"))
            continue
        findings.extend(audit_text(path, text, masker))
    for f in findings:
        sys.stderr.write(f"{f.path}:{f.line}: {f.kind}: {f.excerpt}\n")
    if findings:
        msg = f"fixtures audit failed: {len(findings)} finding(s)"
        raise CaptureError(msg)
    sys.stderr.write(f"fixtures audit passed: {root}\n")
    return root


# --------------------------------------------------------------------------- CLI

SUBCOMMANDS = ("snap", "watch", "herdr-ref", "usage-sample", "audit")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="capture.py", description=__doc__.split("\n\n")[0])
    parser.add_argument("--herdr-session", help="pass --session NAME to every herdr call")
    sub = parser.add_subparsers(dest="command", required=True)

    def sandboxed(p: argparse.ArgumentParser) -> None:
        p.add_argument("--pane", required=True, help="herdr pane id, e.g. w1:p2")
        p.add_argument("--sandbox", type=Path, default=DEFAULT_SANDBOX)
        p.add_argument("--allow-outside-sandbox", action="store_true")

    snap_p = sub.add_parser("snap", help="save one screen (default)")
    sandboxed(snap_p)
    snap_p.add_argument("--agent", type=Agent, required=True, choices=list(Agent))
    snap_p.add_argument("--scenario", type=Scenario, required=True, choices=list(Scenario))
    snap_p.add_argument("--lines", type=int, default=200)
    snap_p.add_argument("--note", default=None, help="free-text note stored in meta.json")
    snap_p.set_defaults(handler=snap)

    watch_p = sub.add_parser("watch", help="record pane.agent_status_changed events")
    sandboxed(watch_p)
    watch_p.add_argument("--agent", type=Agent, choices=list(Agent))
    watch_p.add_argument("--socket", type=Path, help="herdr API socket (default: herdr's lookup)")
    watch_p.add_argument("--max-events", type=int, default=None)
    watch_p.set_defaults(handler=watch)

    ref_p = sub.add_parser("herdr-ref", help="save herdr API schema and detection manifests")
    ref_p.set_defaults(handler=herdr_ref)

    usage_p = sub.add_parser("usage-sample", help="extract a redacted log sample")
    usage_p.add_argument("--agent", type=Agent, required=True, choices=list(Agent))
    usage_p.add_argument("--input", required=True)
    usage_p.add_argument("--out")
    usage_p.add_argument("--per-kind", type=int, default=2)
    usage_p.add_argument("--usage", type=int, default=4, help="extra records carrying usage")
    usage_p.add_argument(
        "--pseudonym-key",
        help="fixed key for id pseudonyms, for a reproducible sample (default: random)",
    )
    usage_p.set_defaults(handler=usage_sample)

    audit_p = sub.add_parser("audit", help="scan fixtures for leaked paths, emails and keys")
    audit_p.add_argument("root", nargs="?", default=str(FIXTURES_DIR))
    audit_p.set_defaults(handler=audit)
    return parser


def with_default_subcommand(argv: list[str]) -> list[str]:
    """Insert ``snap`` when the first non-global argument is not a subcommand."""
    index = 0
    while index < len(argv) and argv[index] == "--herdr-session":
        index += 2
    rest = argv[index:]
    if not rest or rest[0] in SUBCOMMANDS or rest[0] in {"-h", "--help"}:
        return argv
    return [*argv[:index], "snap", *rest]


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(
        with_default_subcommand(sys.argv[1:] if argv is None else argv)
    )
    herdr = Herdr(session=args.herdr_session)
    try:
        result = args.handler(args, herdr, Masker.for_this_machine())
    except CaptureError as err:
        sys.stderr.write(f"{err}\n")
        return 1
    sys.stdout.write(f"{result}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
