"""Masking secrets in text that leaves the machine (notices).

Screens, agent messages and file names can hold anything an agent printed, keys
included. Before such text goes out, known secret shapes and ``key=value`` pairs whose
key names a secret are replaced with ``<redacted>``. The shapes are those the capture
tool masks (tools/capture/capture.py), plus Telegram bot tokens; values the caller
knows to be secret (the bot's own token) are masked wherever they appear.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

REDACTED = "<redacted>"

_SHAPES = (
    # A key cut off by the end of the screen is masked to the end.
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)"
    ),
    re.compile(r"sk-ant-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bsk-(?!ant-)(?:proj-|svcacct-|admin-)?[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}"),
    re.compile(r"\bya29\.[0-9A-Za-z_-]{20,}"),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{22,})"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}"),
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"),  # a Telegram bot token
)
# key=value, key: value, "key": "value" where the key names a secret. Pure numbers are
# kept, so counts such as input_tokens=12345678 survive.
_ASSIGNMENT = re.compile(
    r"(?i)\b([A-Za-z0-9_-]*(?:api[_-]?key|access[_-]?key|token|secret|passwd|password)"
    r"[A-Za-z0-9_-]*)"
    r"([\"']?\s*[:=]\s*[\"']?)"
    r"(?![0-9]+(?:[\"',\s}]|$))(?!<)([^\s\"',;}]{8,})"
)


def redact(text: str, known: Iterable[str] = ()) -> str:
    """``text`` with every secret it holds replaced by ``<redacted>``."""
    for value in known:
        if value:
            text = text.replace(value, REDACTED)
    for shape in _SHAPES:
        text = shape.sub(REDACTED, text)
    return _ASSIGNMENT.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)
