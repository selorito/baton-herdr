"""The screen classifier's answers, pinned (ADR 0011).

Every case of the corpus (``tests/support/detect_corpus.py``: each recorded capture under
each host state, limit messages around daylight-saving changes in seven zones, the fake
agents' screens, edge cases) goes through ``baton-detect classify`` in one run. Each
answer must equal the one in ``tests/golden/classify.jsonl``: the same state and evidence,
and the same reset instant.

The golden answers were first written by the Python detector, when the two
implementations agreed on every case. A deliberate change to the rules changes some
answers; regenerate with ``UPDATE_GOLDEN=1 uv run pytest tests/integration/test_classify_golden.py``
and review the diff.

The mutation test deletes, or loosens, each rule in turn (``classify --rules``) and requires
some answer to change, so that no rule goes untested.

Needs the binary: BATON_DETECT_BIN, else target/debug/baton-detect, else PATH. Skipped
without one, unless BATON_REQUIRE_DETECT is set (CI sets it).
"""

from __future__ import annotations

import json
import os
import subprocess
import tomllib
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from baton_herdr.core.detection import DetectionRequest, DetectionResult

import detect_corpus
from detector_binary import detector

if TYPE_CHECKING:
    from collections.abc import Iterator

GOLDEN = Path(__file__).parents[1] / "golden" / "classify.jsonl"
RULES = Path(__file__).parents[2] / "crates" / "baton-detect" / "rules"

type Answer = tuple[str, str, datetime | None]


def run(requests: list[DetectionRequest], *args: str) -> list[DetectionResult]:
    result = subprocess.run(  # noqa: S603 - the binary under test, fixed arguments
        [str(detector()), "classify", *args],
        input="".join(r.model_dump_json() + "\n" for r in requests),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return [DetectionResult.model_validate_json(line) for line in result.stdout.splitlines()]


def answer(result: DetectionResult) -> Answer:
    return (result.state.value, result.evidence, result.resets_at)


def golden() -> dict[str, Answer]:
    answers = {}
    for line in GOLDEN.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        resets = datetime.fromisoformat(row["resets_at"]) if row["resets_at"] else None
        answers[row["case"]] = (row["state"], row["evidence"], resets)
    return answers


@pytest.fixture(scope="module")
def corpus() -> tuple[list[str], list[DetectionRequest]]:
    cases = detect_corpus.cases()
    return [c["name"] for c in cases], [detect_corpus.request_of(c) for c in cases]


def test_every_case_is_answered_as_recorded(
    corpus: tuple[list[str], list[DetectionRequest]],
) -> None:
    names, requests = corpus
    answers = [answer(r) for r in run(requests)]
    if os.environ.get("UPDATE_GOLDEN"):
        GOLDEN.write_text(
            "".join(
                json.dumps(
                    {
                        "case": name,
                        "state": state,
                        "evidence": evidence,
                        "resets_at": resets.isoformat() if resets else None,
                    },
                    ensure_ascii=False,
                )
                + "\n"
                for name, (state, evidence, resets) in zip(names, answers, strict=True)
            ),
            encoding="utf-8",
        )
    expected = golden()
    assert set(expected) == set(names), "cases were added or removed: regenerate the golden"
    differences = [
        f"{name}: expected {expected[name]}, got {got}"
        for name, got in zip(names, answers, strict=True)
        if got != expected[name]
    ]
    assert not differences, f"{len(differences)} of {len(names)} differ:\n" + "\n".join(
        differences[:20]
    )


def test_the_golden_reaches_every_rule_and_state() -> None:
    answers = golden().values()
    evidence = {e for _, e, _ in answers}
    for path in sorted(RULES.glob("*.toml")):
        ids = {f"baton:{rule['id']}" for rule in tomllib.loads(path.read_text())["rules"]}
        assert not ids - evidence, (path.name, ids - evidence)
    assert {s for s, _, _ in answers} >= {"working", "idle", "rate_limited", "blocked_other"}
    assert any(r for _, _, r in answers)


def _toml(spec: dict[str, Any]) -> str:
    """A rules file, written back in the subset of TOML the rules use."""

    def value(v: object) -> str:
        if isinstance(v, list):
            return "[" + ", ".join(value(x) for x in v) + "]"
        text = str(v)
        assert "'''" not in text
        return f"'''{text}'''" if "\n" in text or "'" in text or "\\" in text else f"'{text}'"

    lines = [f"agent = {value(spec['agent'])}"]
    if spec.get("refine_blocked"):
        lines.append("[refine_blocked]")
        lines += [f"{key} = {value(v)}" for key, v in spec["refine_blocked"].items()]
    for rule in spec["rules"]:
        lines.append("[[rules]]")
        lines += [f"{key} = {value(v)}" for key, v in rule.items()]
    return "\n".join(lines) + "\n"


def _mutants() -> Iterator[tuple[str, str, dict[str, Any]]]:
    """Each rule deleted, and each rule's host-state restriction dropped."""
    for path in sorted(RULES.glob("*.toml")):
        spec = tomllib.loads(path.read_text())
        rules = spec["rules"]
        for n, rule in enumerate(rules):
            yield path.name, f"without {rule['id']}", spec | {"rules": rules[:n] + rules[n + 1 :]}
            if "applies_when" in rule:
                loose = {k: v for k, v in rule.items() if k != "applies_when"}
                name = f"{rule['id']} in every host state"
                yield path.name, name, spec | {"rules": [*rules[:n], loose, *rules[n + 1 :]]}


def test_the_rules_written_back_are_the_rules(tmp_path: Path) -> None:
    for path in RULES.glob("*.toml"):
        written = tmp_path / path.name
        written.write_text(_toml(tomllib.loads(path.read_text())), encoding="utf-8")
        assert tomllib.loads(written.read_text()) == tomllib.loads(path.read_text())


def test_every_rule_mutant_changes_some_answer(
    tmp_path: Path, corpus: tuple[list[str], list[DetectionRequest]]
) -> None:
    names, requests = corpus
    expected = golden()
    survivors = []
    for file, name, spec in _mutants():
        mutant = tmp_path / name.replace(" ", "-")
        mutant.mkdir()
        for path in RULES.glob("*.toml"):
            text = _toml(spec) if path.name == file else path.read_text()
            (mutant / path.name).write_text(text, encoding="utf-8")
        got = [answer(r) for r in run(requests, "--rules", str(mutant))]
        if all(g == expected[n] for n, g in zip(names, got, strict=True)):
            survivors.append(f"{file}: {name}")
    assert not survivors, survivors


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ('{"agent": "claude"}', "line 1: invalid detection request"),
        (
            '{"agent": "codex", "screen": "", "observed_at": "2026-10-01T11:00:00Z",'
            ' "timezone": "Mars/Base"}',
            'line 1: unknown time zone "Mars/Base"',
        ),
        (
            '{"contract": 2, "agent": "codex", "screen": "",'
            ' "observed_at": "2026-10-01T11:00:00Z"}',
            "contract 2 is not supported",
        ),
    ],
)
def test_a_request_that_cannot_be_classified_stops_the_binary(line: str, message: str) -> None:
    result = subprocess.run(  # noqa: S603 - the binary under test, fixed arguments
        [str(detector()), "classify"],
        input=line + "\n",
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 2
    assert message in result.stderr
