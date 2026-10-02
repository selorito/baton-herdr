"""Command-line entry point for ``coban``."""

from __future__ import annotations

import sys
from importlib.metadata import version as package_version

import typer
from pydantic import ValidationError

from coban.adapters import ADAPTERS
from coban.core.detection import DetectionRequest, DetectionResult

app = typer.Typer(name="coban", no_args_is_help=True, add_completion=False)


@app.callback()
def main() -> None:
    """Orchestrate coding agents running on herdr."""


@app.command()
def version() -> None:
    """Print the coban version."""
    typer.echo(f"coban {package_version('coban')}")


@app.command()
def detect() -> None:
    """Classify screens: one DetectionRequest JSON per stdin line, one result per stdout line.

    This is the detector's NDJSON contract (schemas/detector-*.v1.json).
    """
    for number, line in enumerate(sys.stdin, start=1):
        if not line.strip():
            continue
        try:
            request = DetectionRequest.model_validate_json(line)
        except ValidationError as err:
            typer.echo(f"line {number}: invalid detection request: {err}", err=True)
            raise typer.Exit(code=2) from err
        adapter = ADAPTERS.get(request.agent)
        result = (
            adapter.classify(request)
            if adapter is not None
            else DetectionResult(state=request.host_state, evidence="coban:no-adapter")
        )
        sys.stdout.write(result.model_dump_json() + "\n")
        sys.stdout.flush()
