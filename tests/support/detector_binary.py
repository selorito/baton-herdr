"""Where the tests find baton-detect: BATON_DETECT_BIN, else target/debug, else PATH.

Without a binary the test is skipped, unless BATON_REQUIRE_DETECT is set (the CI parity
job sets it), in which case it fails.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

REPO = Path(__file__).parents[2]


def detector() -> Path:
    candidates = [
        os.environ.get("BATON_DETECT_BIN"),
        str(REPO / "target" / "debug" / "baton-detect"),
        shutil.which("baton-detect"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    if os.environ.get("BATON_REQUIRE_DETECT"):
        pytest.fail("baton-detect is required but was not found")
    pytest.skip("baton-detect is not built")
