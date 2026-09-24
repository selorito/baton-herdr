"""Command-line entry point for ``coban``."""

from __future__ import annotations

from importlib.metadata import version as package_version

import typer

app = typer.Typer(name="coban", no_args_is_help=True, add_completion=False)


@app.callback()
def main() -> None:
    """Orchestrate coding agents running on herdr."""


@app.command()
def version() -> None:
    """Print the coban version."""
    typer.echo(f"coban {package_version('coban')}")
