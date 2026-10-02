"""JSON Schemas of the external contracts, generated from the models.

The committed files in ``schemas/`` are the contract. A test fails when the
models drift from them; regenerate deliberately with ``just schemas``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from baton_herdr.core.detection import DetectionRequest, DetectionResult

if TYPE_CHECKING:
    from pathlib import Path

CONTRACTS: dict[str, Any] = {
    "detector-request.v1.json": DetectionRequest,
    "detector-result.v1.json": DetectionResult,
}


def render(model: Any) -> str:
    return json.dumps(model.model_json_schema(), indent=2, sort_keys=True) + "\n"


def write_all(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name, model in CONTRACTS.items():
        (directory / name).write_text(render(model), encoding="utf-8")
