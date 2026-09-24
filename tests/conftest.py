from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep the developer's own COBAN_* variables and .env out of every test."""
    for key in list(os.environ):
        if key.startswith("COBAN_"):
            monkeypatch.delenv(key)
    monkeypatch.chdir(tmp_path)
