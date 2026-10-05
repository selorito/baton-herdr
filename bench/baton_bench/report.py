"""Sum up the worlds of each scenario and write them as Markdown.

Every figure is rounded to a fixed number of places, so the same results always give
the same text.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from baton_bench.sim import median, percentile
from baton_herdr.core.model import TaskStatus

if TYPE_CHECKING:
    from collections.abc import Sequence

    from baton_bench.sim import WorldResult


@dataclass(frozen=True, slots=True)
class Summary:
    worlds: int
    tasks: int
    completed: int
    needs_person: int
    unfinished: int
    interruptions: int
    redone: int
    tokens: int
    wasted: int
    overhead: int
    limits: int
    detection: dict[str, list[float]]
    recovery: dict[str, list[float]]
    after_reset: list[float]
    makespans: list[float]

    @property
    def completed_share(self) -> float:
        return self.completed / self.tasks


def summarize(results: Sequence[WorldResult]) -> Summary:
    tasks = [task for world in results for task in world.tasks]
    detection: dict[str, list[float]] = {}
    recovery: dict[str, list[float]] = {}
    for world in results:
        for kind, seconds in world.detection:
            detection.setdefault(kind, []).append(seconds)
        for kind, seconds in world.recovery:
            recovery.setdefault(kind, []).append(seconds)
    completed = sum(1 for t in tasks if t.status is TaskStatus.COMPLETED)
    needs_person = sum(1 for t in tasks if t.needs_person)
    return Summary(
        worlds=len(results),
        tasks=len(tasks),
        completed=completed,
        needs_person=needs_person,
        unfinished=len(tasks) - completed - needs_person,
        interruptions=sum(len(w.detection) for w in results),
        redone=sum(t.redone for t in tasks),
        tokens=sum(t.tokens for t in tasks),
        wasted=sum(t.wasted for t in tasks),
        overhead=sum(t.overhead for t in tasks),
        limits=sum(w.limits for w in results),
        detection=detection,
        recovery=recovery,
        after_reset=[s for w in results for s in w.after_reset],
        makespans=[w.makespan_s for w in results],
    )


def seconds(value: float) -> str:
    if math.isnan(value):  # nothing to measure
        return "–"
    if value < 120:
        return f"{value:.1f} s"
    if value < 7_200:
        return f"{value / 60:.1f} min"
    return f"{value / 3_600:.2f} h"


def tokens(value: float) -> str:
    if abs(value) >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if abs(value) >= 1_000:
        return f"{value / 1_000:.1f}k"
    return f"{value:.0f}"


def share(part: float, whole: float) -> str:
    return f"{100 * part / whole:.1f} %" if whole else "–"


def scenario_table(rows: Sequence[tuple[str, Summary]]) -> list[str]:
    lines = [
        "| Scenario | Tasks | Done on their own | Need a person | Detection (median / max) "
        "| Stop → work goes on (median / p95) | Redone steps per stop | Tokens lost |",
        "|---|---:|---:|---:|---|---|---:|---:|",
    ]
    for title, s in rows:
        detection = [v for values in s.detection.values() for v in values]
        recovery = [v for values in s.recovery.values() for v in values]
        per_stop = f"{s.redone / s.interruptions:.2f}" if s.interruptions else "–"
        lines.append(
            f"| {title} | {s.tasks} | {share(s.completed, s.tasks)} "
            f"| {share(s.needs_person, s.tasks)} "
            f"| {seconds(median(detection))} / {seconds(max(detection, default=float('nan')))} "
            f"| {seconds(median(recovery))} / {seconds(percentile(recovery, 0.95))} "
            f"| {per_stop} | {share(s.wasted, s.tokens)} |"
        )
    return lines


def waits_table(rows: Sequence[tuple[str, Summary]]) -> list[str]:
    lines = [
        "| Scenario | Waits | Reset → work goes on (median / max) |",
        "|---|---:|---|",
    ]
    for title, s in rows:
        if not s.after_reset:
            continue
        lines.append(
            f"| {title} | {len(s.after_reset)} | {seconds(median(s.after_reset))} "
            f"/ {seconds(max(s.after_reset))} |"
        )
    return lines


def budget_table(rows: Sequence[tuple[str, str, Summary]]) -> list[str]:
    base = rows[0][2]
    lines = [
        "| Mode | Tasks done | Limits hit | Redone steps | Tokens (counted) | vs. off "
        "| Lost + re-reading | Queue finished in (median) |",
        "|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for name, what, s in rows:
        delta = (s.tokens - base.tokens) / base.tokens
        lines.append(
            f"| `{name}`: {what} | {share(s.completed, s.tasks)} | {s.limits} | {s.redone} "
            f"| {tokens(s.tokens)} | {delta:+.1%} | {share(s.wasted + s.overhead, s.tokens)} "
            f"| {seconds(median(s.makespans))} |"
        )
    return lines
