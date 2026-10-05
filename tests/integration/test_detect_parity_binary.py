"""The Rust screen classifier against the Python one (ADR 0011, phase 2: parity).

Every case of the corpus (``tests/support/detect_corpus.py``) goes through
``baton-detect classify`` in one run; each answer must be a valid ``DetectionResult``
and the same as Python's: the same state and evidence, and the same reset instant.
Reset times are compared as instants: Python keeps the observation's UTC offset when it
adds a duration, Rust writes UTC.

Needs the binary: BATON_DETECT_BIN, else target/debug/baton-detect, else PATH. Skipped
without one, unless BATON_REQUIRE_DETECT is set (the CI parity job sets it).
"""

from __future__ import annotations

import dataclasses
import importlib
import subprocess

import pytest

from baton_herdr.core.detection import DetectionRequest, DetectionResult

import detect_corpus
from detector_binary import detector


def run(requests: list[DetectionRequest]) -> list[str]:
    result = subprocess.run(  # noqa: S603 - the binary under test, fixed arguments
        [str(detector()), "classify"],
        input="".join(r.model_dump_json() + "\n" for r in requests),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.splitlines()


def test_rust_classifies_every_case_as_python_does() -> None:
    cases = detect_corpus.cases()
    requests = [detect_corpus.request_of(case) for case in cases]
    lines = run(requests)
    assert len(lines) == len(cases)

    differences = []
    for case, request, line in zip(cases, requests, lines, strict=True):
        rust = DetectionResult.model_validate_json(line)  # the contract
        python = detect_corpus.python_classify(request)
        if (rust.state, rust.evidence, rust.resets_at) != (
            python.state,
            python.evidence,
            python.resets_at,
        ):
            differences.append(f"{case['name']}: python {python!r}, rust {rust!r}")
    assert not differences, f"{len(differences)} of {len(cases)} differ:\n" + "\n".join(
        differences[:20]
    )


def test_the_corpus_reaches_every_rule_and_state() -> None:
    results = [
        detect_corpus.python_classify(detect_corpus.request_of(case))
        for case in detect_corpus.cases()
    ]
    evidence = {r.evidence for r in results}
    for module in ("claude", "codex", "opencode"):
        rules = __import__(f"baton_herdr.adapters.{module}", fromlist=["RULES"]).RULES
        missing = {f"baton:{rule.rule_id}" for rule in rules} - evidence
        assert not missing, missing
    assert {r.state for r in results} == set(type(results[0].state))
    assert any(r.resets_at for r in results)


def _mutants() -> list[tuple[str, str, tuple[object, ...]]]:
    """Each rule deleted, and each rule's host-state restriction dropped."""
    mutants = []
    for module in ("claude", "codex", "opencode"):
        rules = importlib.import_module(f"baton_herdr.adapters.{module}").RULES
        for n, rule in enumerate(rules):
            mutants.append((module, f"without {rule.rule_id}", rules[:n] + rules[n + 1 :]))
            if rule.applies_when is not None:
                loose = dataclasses.replace(rule, applies_when=None)
                name = f"{rule.rule_id} in every host state"
                mutants.append((module, name, (*rules[:n], loose, *rules[n + 1 :])))
    return mutants


def test_the_corpus_tells_every_rule_mutant_from_rust(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutation check: with any one Python rule broken, parity fails.

    So every rule is pinned by at least one case that only it decides, and a rule
    changed on one side alone cannot slip through the parity test.
    """
    cases = detect_corpus.cases()
    requests = [detect_corpus.request_of(case) for case in cases]
    rust = [DetectionResult.model_validate_json(line) for line in run(requests)]
    survivors = []
    for module, name, rules in _mutants():
        with monkeypatch.context() as patch:
            patch.setattr(f"baton_herdr.adapters.{module}.RULES", rules)
            killed = any(
                (r.state, r.evidence, r.resets_at) != (p.state, p.evidence, p.resets_at)
                for r, p in zip(rust, map(detect_corpus.python_classify, requests), strict=True)
            )
        if not killed:
            survivors.append(f"{module}: {name}")
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
