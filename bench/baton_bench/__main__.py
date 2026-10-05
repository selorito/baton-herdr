"""``just bench``: run every scenario and write ``bench/results/<date>.md``.

    python -m baton_bench [--seed N] [--worlds N] [--out DIR]

The same seed gives the same file, apart from its name.
"""

from __future__ import annotations

import argparse
import functools
import sys
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING

from baton_bench import report, scenarios
from baton_bench.sim import simulate
from baton_herdr.core.logging import configure_logging

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from random import Random

    from baton_bench.sim import Setup, WorldResult

RESULTS = Path(__file__).parents[1] / "results"


def _quiet() -> None:
    configure_logging("error")


def _worlds(
    pool: ProcessPoolExecutor, build: Callable[[Random], Setup], name: str, seed: int, count: int
) -> list[WorldResult]:
    seeds = [f"{seed}/{name}/{n}" for n in range(count)]
    return list(pool.map(functools.partial(simulate, build), seeds))


def run(seed: int, worlds: int | None) -> str:
    with ProcessPoolExecutor(initializer=_quiet) as pool:
        recovery = [
            (s.title, report.summarize(_worlds(pool, s.build, s.name, seed, worlds or s.worlds)))
            for s in scenarios.SCENARIOS
        ]
        budget = [
            (
                name,
                what,
                report.summarize(
                    _worlds(
                        pool,
                        functools.partial(scenarios.queue, budget=budget),
                        "queue",  # the same worlds in every mode
                        seed,
                        worlds or scenarios.BUDGET_WORLDS,
                    )
                ),
            )
            for name, what, budget in scenarios.BUDGET_MODES
        ]
    return render(seed, recovery, budget)


def render(
    seed: int,
    recovery: Sequence[tuple[str, report.Summary]],
    budget: Sequence[tuple[str, str, report.Summary]],
) -> str:
    worlds = sum(s.worlds for _, s in recovery)
    lines = [
        "# baton benchmark",
        "",
        f"Seed {seed}, baton {version('baton-herdr')}. Produced by `just bench`; the model, "
        "the scenarios and what each figure means are in [bench/README.md](../README.md).",
        "Times are simulated: baton's own scheduler, detection and recovery run against "
        "simulated agents, on a clock that jumps ahead whenever everything waits.",
        "",
        f"## Recovery ({worlds} worlds, one task each)",
        "",
        *(f"- **{s.title}**: {s.what}" for s in scenarios.SCENARIOS),
        "",
        *report.scenario_table(recovery),
        "",
        "### Waiting for a limit to reset",
        "",
        *report.waits_table(recovery),
        "",
        f"## Budget-aware choice ({budget[0][2].worlds} worlds, "
        f"{scenarios.QUEUE} tasks each, the same worlds in every mode)",
        "",
        f"Claude's window ({report.tokens(scenarios.CLAUDE_CAP)} counted tokens) is 40–80 % "
        f"used, in a window that opened 1–3 h before; Codex's "
        f"({report.tokens(scenarios.CODEX_CAP)}) is 20–50 % used. Claude is preferred.",
        "",
        *report.budget_table(budget),
        "",
    ]
    return "\n".join(lines)


def _free_path(directory: Path, stem: str, text: str) -> Path:
    """``<date>.md``; a later run that day with other results gets ``<date>-2.md`` and so on,
    so earlier results are never overwritten. The same results reuse their file."""
    n = 1
    while True:
        path = directory / (f"{stem}.md" if n == 1 else f"{stem}-{n}.md")
        if not path.exists() or path.read_text(encoding="utf-8") == text:
            return path
        n += 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="baton_bench", description=__doc__)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--worlds", type=int, help="worlds per scenario (default: each its own)")
    parser.add_argument("--out", type=Path, default=RESULTS)
    args = parser.parse_args(argv)
    text = run(args.seed, args.worlds)
    args.out.mkdir(parents=True, exist_ok=True)
    path = _free_path(args.out, datetime.now(tz=UTC).date().isoformat(), text)
    path.write_text(text, encoding="utf-8")
    sys.stdout.write(f"{path}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
