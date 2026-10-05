"""The scenarios: each builds one world from a seeded random generator.

Numbers are chosen once, to be plausible, and are not tuned to any result:

- a task has 6 to 12 steps; a step takes 40 to 120 s and about 7,500 counted tokens;
- an agent that has its quota to itself never stops on its own;
- baton runs with its default settings (``SchedulerSettings``, ``BudgetSettings``),
  with usage collection on unless a scenario says otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from baton_bench.sim import START, Budget, Setup
from baton_bench.world import Fault, Quota, Workdir
from baton_herdr.core.model import AgentKind

if TYPE_CHECKING:
    import random
    from collections.abc import Callable

CLAUDE, CODEX = AgentKind.CLAUDE, AgentKind.CODEX
# More than any world here can spend.
PLENTY = 100_000_000
# Counted tokens of an average step (Profile's ranges: input + cache writes + output).
STEP_TOKENS = 1_750 + 5_000 + 1_400


@dataclass(frozen=True, slots=True)
class Scenario:
    name: str
    title: str
    what: str
    build: Callable[[random.Random], Setup]
    worlds: int = 50


def _task(rng: random.Random, *faults: tuple[str, float]) -> Workdir:
    """A task of 6 to 12 steps; each fault strikes at a random point of it."""
    steps = rng.randint(6, 12)
    placed = [Fault(kind, min(steps - 1, int(at * steps))) for kind, at in faults]
    return Workdir(steps=steps, faults=placed)


def _used(
    rng: random.Random, cap: int, share: tuple[float, float], age_h: tuple[float, float]
) -> Quota:
    """A quota already partly used, in a window that opened some hours ago."""
    start = START - timedelta(hours=rng.uniform(*age_h))
    return Quota(cap=cap, start=start, used=round(cap * rng.uniform(*share)))


def limit_mid_task(rng: random.Random) -> Setup:
    """Claude runs out partway through; Codex has room."""
    task = _task(rng)
    # Claude has room for 20-80 % of the task's steps in its window.
    room = round(rng.uniform(0.2, 0.8) * task.steps * STEP_TOKENS)
    claude = Quota(cap=1_000_000, start=START - timedelta(hours=1), used=1_000_000 - room)
    return Setup(tasks=[task], quotas={CLAUDE: claude, CODEX: Quota(cap=PLENTY)})


def crash(rng: random.Random) -> Setup:
    """Claude's process dies partway through, once, twice or three times."""
    count = rng.choices([1, 2, 3], weights=[6, 3, 1])[0]
    task = _task(rng, *(("crash", rng.random()) for _ in range(count)))
    task.faults.sort(key=lambda f: f.step)
    return Setup(tasks=[task], quotas={CLAUDE: Quota(cap=PLENTY), CODEX: Quota(cap=PLENTY)})


def hang(rng: random.Random) -> Setup:
    """Claude freezes partway through: the screen still says it works, nothing moves."""
    task = _task(rng, ("hang", rng.random()))
    return Setup(tasks=[task], quotas={CLAUDE: Quota(cap=PLENTY), CODEX: Quota(cap=PLENTY)})


def consecutive_limits(rng: random.Random) -> Setup:
    """Claude runs out, then Codex does too; Claude's window ends first."""
    task = _task(rng)
    total = task.steps * STEP_TOKENS
    claude_room = round(rng.uniform(0.15, 0.4) * total)
    codex_room = round(rng.uniform(0.15, 0.4) * total)
    claude = Quota(cap=1_000_000, start=START - timedelta(hours=rng.uniform(4, 4.5)))
    claude.used = claude.cap - claude_room
    codex = Quota(cap=2_000_000, start=START - timedelta(hours=rng.uniform(1, 2)))
    codex.used = codex.cap - codex_room
    return Setup(tasks=[task], quotas={CLAUDE: claude, CODEX: codex})


def all_limited(rng: random.Random) -> Setup:
    """Claude runs out partway through and Codex has nothing left: the task must wait."""
    task = _task(rng)
    room = round(rng.uniform(0.2, 0.8) * task.steps * STEP_TOKENS)
    claude = Quota(cap=1_000_000, start=START - timedelta(hours=rng.uniform(2, 4)))
    claude.used = claude.cap - room
    codex = Quota(cap=2_000_000, start=START - timedelta(hours=rng.uniform(1, 4)), used=2_000_000)
    return Setup(tasks=[task], quotas={CLAUDE: claude, CODEX: codex})


SCENARIOS = (
    Scenario(
        "limit-mid-task",
        "Limit in the middle of a task",
        "Claude hits its session limit partway through; Codex has room.",
        limit_mid_task,
    ),
    Scenario(
        "crash",
        "Crash",
        "Claude's process dies partway through, 1 to 3 times (weights 6:3:1); baton resumes "
        "a session at most twice (`max_failure_resumes`), then asks a person.",
        crash,
    ),
    Scenario(
        "hang",
        "Hang",
        "Claude freezes partway through; its screen keeps saying it works.",
        hang,
    ),
    Scenario(
        "consecutive-limits",
        "Consecutive limits",
        "Claude runs out, the task moves to Codex, Codex runs out too; Claude's window ends first.",
        consecutive_limits,
    ),
    Scenario(
        "all-limited",
        "Every agent limited",
        "Claude runs out partway through and Codex's window is used up: the task waits for "
        "the first reset.",
        all_limited,
    ),
)


# --- budget-aware choice ---------------------------------------------------------------

# A queue of tasks, Claude's window already well used, Codex with room: does knowing the
# budget save the tokens a mid-task limit wastes?
QUEUE = 10
CLAUDE_CAP = 600_000
CODEX_CAP = 3_000_000


def queue(rng: random.Random, budget: Budget) -> Setup:
    tasks = [_task(rng) for _ in range(QUEUE)]
    claude = _used(rng, CLAUDE_CAP, (0.4, 0.8), (1, 3))
    codex = _used(rng, CODEX_CAP, (0.2, 0.5), (1, 4))
    return Setup(tasks=tasks, quotas={CLAUDE: claude, CODEX: codex}, budget=budget)


BUDGET_MODES = (
    ("off", "No usage data (`[usage] enabled = false`)", Budget(collect_usage=False)),
    ("learned", "Usage collected, Claude's cap learned from its limits (the default)", Budget()),
    (
        "configured",
        "Usage collected, `claude_window_tokens` set, 10 % reserve",
        Budget(configured_window=True),
    ),
    (
        "configured-20",
        "As above, 20 % reserve",
        Budget(configured_window=True, reserve_percent=20),
    ),
)
BUDGET_WORLDS = 30
