"""Tests that exercise a POSIX facility on purpose, skipped on native Windows.

Each says which facility. What baton does instead on Windows is tested on its own
(``test_herdr_pipe_windows.py``, ``test_wake.py``, ``test_service.py``).
"""

from __future__ import annotations

import sys

import pytest


def posix_only(why: str) -> pytest.MarkDecorator:
    return pytest.mark.skipif(sys.platform == "win32", reason=f"POSIX only: {why}")


def linux_only(why: str) -> pytest.MarkDecorator:
    return pytest.mark.skipif(not sys.platform.startswith("linux"), reason=f"Linux only: {why}")
