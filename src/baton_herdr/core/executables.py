"""Finding the programs baton runs, by name or by path."""

from __future__ import annotations

import os
import shutil
from pathlib import Path


def detector_binary(name: str) -> Path | None:
    """``name`` as a path to an executable, or found on PATH."""
    if os.sep in name:
        path = Path(name).expanduser()
        return path if path.is_file() and os.access(path, os.X_OK) else None
    found = shutil.which(name)
    return Path(found) if found else None
