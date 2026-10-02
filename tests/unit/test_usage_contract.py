from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from baton_herdr.core.usage import USAGE_EVENT, RateLimitObservation, UsageRecord

USAGE = (
    '{"kind":"usage","agent":"claude","session_id":"s1","record_id":"claude:s1:msg_1",'
    '"at":"2026-09-30T07:15:26.612Z","model":"claude-opus-5-5",'
    '"tokens":{"input":2,"output":139,"cache_read":23176,"cache_write":11055,"reasoning":null}}'
)
LIMITS = (
    '{"kind":"rate_limits","agent":"codex","session_id":"s2","at":"2026-09-30T07:56:58.33Z",'
    '"windows":[{"name":"primary","window_minutes":300,"used_percent":1.0,'
    '"resets_at":"2026-09-30T12:50:50Z"}]}'
)


def test_lines_are_told_apart_by_kind() -> None:
    usage = USAGE_EVENT.validate_json(USAGE)
    assert isinstance(usage, UsageRecord)
    assert usage.tokens.cache_write == 11055
    assert usage.at == datetime(2026, 9, 30, 7, 15, 26, 612000, tzinfo=UTC)
    limits = USAGE_EVENT.validate_json(LIMITS)
    assert isinstance(limits, RateLimitObservation)
    assert limits.windows[0].window_minutes == 300


@pytest.mark.parametrize(
    "line",
    [
        USAGE.replace('"kind":"usage"', '"kind":"other"'),
        USAGE.replace('"input":2', '"input":-2'),
        USAGE.replace('"agent":"claude"', '"agent":"gemini"'),
        USAGE.replace('Z"', '"', 1),  # a time without a zone
        USAGE.replace('"model"', '"extra":1,"model"'),
        LIMITS.replace('"windows":[{', '"windows":[],"x":[{'),
    ],
)
def test_anything_off_contract_is_rejected(line: str) -> None:
    with pytest.raises(ValidationError):
        USAGE_EVENT.validate_json(line)
