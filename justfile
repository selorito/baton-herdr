# Run `just` to list recipes.

set shell := ["bash", "-euo", "pipefail", "-c"]

default:
    @just --list

# Every check CI runs. Must be green before merging.
check: check-py check-rs fixtures-audit

# Python: lint, format, types, import boundaries, tests with coverage.
check-py:
    uv sync --locked
    uv run ruff check .
    uv run ruff format --check .
    uv run mypy
    uv run lint-imports
    uv run pytest --cov --cov-report=term

# Rust: format, clippy with warnings as errors, tests.
check-rs:
    cargo fmt --all --check
    cargo clippy --workspace --all-targets --locked -- -D warnings
    cargo test --workspace --locked

# Fail if anything under fixtures/ contains a path outside the sandbox, an email or a key.
fixtures-audit:
    uv run tools/capture/capture.py audit fixtures

# Regenerate fixtures/herdr from the installed herdr. Needs a running server;
# pass e.g. `--herdr-session NAME` to target a named session.
fixtures-herdr *ARGS:
    uv run tools/capture/capture.py {{ARGS}} herdr-ref

# Live smoke test against the installed herdr, in a throwaway session. Not part of check or CI.
smoke:
    uv run pytest -m live -v

# Regenerate the JSON Schemas in schemas/ after deliberately changing a contract model.
schemas:
    uv run python -c "from pathlib import Path; from coban.core.schemas import write_all; write_all(Path('schemas'))"

# Apply formatting and safe lint fixes.
fmt:
    uv run ruff check --fix .
    uv run ruff format .
    cargo fmt --all

# Run the test suites only.
test:
    uv run pytest
    cargo test --workspace --locked
