# Run `just` to list recipes.

set shell := ["bash", "-euo", "pipefail", "-c"]

default:
    @just --list

# Every check CI runs. Must be green before merging.
check: check-py check-rs

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

# Apply formatting and safe lint fixes.
fmt:
    uv run ruff check --fix .
    uv run ruff format .
    cargo fmt --all

# Run the test suites only.
test:
    uv run pytest
    cargo test --workspace --locked
