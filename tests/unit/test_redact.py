from __future__ import annotations

import pytest

from baton_herdr.core.redact import REDACTED, redact


@pytest.mark.parametrize(
    "secret",
    [
        "sk-ant-api03-abcdefghijklmnopqrstuv",
        "sk-proj-abcdefghijklmnopqrstuvwx",
        "AIzaSyA1234567890abcdefghijklmnopqrstuv",
        "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        "github_pat_11ABCDEFGHIJKLMNOPQRSTUV",
        "xoxb-1234567890-abcdefghij",
        "AKIAABCDEFGHIJKLMNOP",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.abcdefghijklmn",
        "Bearer abcdefghijklmnop1234",
        "1234567890:AAHabcdefghijklmnopqrstuvwxyz01234",
        "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA\n",
    ],
)
def test_known_secret_shapes_are_masked(secret: str) -> None:
    assert redact(f"before {secret} after").startswith(f"before {REDACTED}")
    assert secret.rsplit(maxsplit=1)[-1] not in redact(f"before {secret} after")


def test_assignments_lose_their_value_and_counts_survive() -> None:
    text = 'export DB_PASSWORD="hunter2hunter2"; input_tokens=12345678 api_key: abcdefgh12'
    assert redact(text) == (
        f'export DB_PASSWORD="{REDACTED}"; input_tokens=12345678 api_key: {REDACTED}'
    )


def test_values_known_to_be_secret_are_masked_anywhere() -> None:
    assert redact("x opaque-value y", known=("opaque-value", "")) == f"x {REDACTED} y"
    assert redact("nothing to hide in 3 files") == "nothing to hide in 3 files"
