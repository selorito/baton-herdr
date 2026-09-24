# Run `just` to list recipes.

set shell := ["bash", "-euo", "pipefail", "-c"]

default:
    @just --list

# Every check CI runs. Must be green before merging.
check: check-py

# Python: lint, format, types, tests with coverage.
check-py:
    uv sync --locked
    uv run ruff check .
    uv run ruff format --check .
    uv run mypy
    uv run pytest --cov --cov-report=term

# Apply formatting and safe lint fixes.
fmt:
    uv run ruff check --fix .
    uv run ruff format .

# Run the test suites only.
test:
    uv run pytest
