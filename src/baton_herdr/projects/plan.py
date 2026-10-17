"""What the agents are asked, and reading the plan back (ADR 0016). Pure.

A project starts with a plan task: the agent is asked to write ``.baton/plan.json`` and
nothing else. Each approved step then becomes a task whose prompt carries the request
and the plan around the step, because the agent of a step has seen neither.

A step may name the earlier steps it builds on (``needs``, ADR 0017). Steps that do not
build on each other run at the same time, each in a worktree of its own.
"""

from __future__ import annotations

import json

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from baton_herdr.core.contract import one_line
from baton_herdr.core.events import PlanStep

PLAN_FILE = ".baton/plan.json"
MAX_STEPS = 8


class Plan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    summary: str = Field(default="", max_length=2000)
    steps: tuple[PlanStep, ...] = Field(min_length=1, max_length=MAX_STEPS)

    @model_validator(mode="after")
    def _needs_earlier_steps(self) -> Plan:
        for index, step in enumerate(self.steps, start=1):
            if step.needs is not None and any(n >= index for n in step.needs):
                msg = f"step {index} may only need earlier steps, not {list(step.needs)}"
                raise ValueError(msg)
        return self

    def needs(self, index: int) -> tuple[int, ...]:
        """The steps step ``index`` builds on: those it names, or else the step before."""
        named = self.steps[index - 1].needs
        if named is not None:
            return tuple(sorted(set(named)))
        return (index - 1,) if index > 1 else ()

    def before(self, index: int) -> frozenset[int]:
        """Every step step ``index`` builds on, directly or through others."""
        found: set[int] = set()
        todo = list(self.needs(index))
        while todo:
            n = todo.pop()
            if n not in found:
                found.add(n)
                todo.extend(self.needs(n))
        return frozenset(found)

    def alongside(self, index: int) -> tuple[int, ...]:
        """The steps that may run at the same time as step ``index``."""
        return tuple(
            n
            for n in range(1, len(self.steps) + 1)
            if n != index and n not in self.before(index) and index not in self.before(n)
        )

    @property
    def parallel(self) -> bool:
        """Whether any two steps may run at the same time."""
        return any(self.alongside(n) for n in range(1, len(self.steps) + 1))


class PlanError(Exception):
    """The plan file is missing or not a plan; the message says what is wrong."""


def plan_title(request: str) -> str:
    return _short(f"Plan: {request}", 80)


def plan_prompt(request: str) -> str:
    """The plan task's prompt: plan, do not build."""
    return one_line(
        "You are planning a project, not building it. Do not create or change any file "
        f"except {PLAN_FILE}, and do not run anything that changes this directory. "
        f"The request, from the person this project is for: {request} "
        f"Write {PLAN_FILE} as JSON with this shape: "
        '{"summary": "what will be built, with which tools, in a short paragraph", '
        '"steps": [{"title": "a short name", "instructions": "what to do in this step", '
        '"needs": [1]}]}. '
        f"Use 1 to {MAX_STEPS} steps, in an order they can be done in. Each step is done "
        "later by another agent who sees only the request, the summary, the list of step "
        "titles and that step's instructions, so make the instructions complete. Each step "
        "should leave something that can be checked on its own. "
        '"needs" lists the numbers of the earlier steps a step builds on; use [] for a step '
        "that builds on none. Steps that do not need each other are done at the same time "
        "by different agents, each in a copy of the directory, and then merged, so the work "
        "finishes sooner: split the work into such steps where you can, and give steps "
        "that run at the same time different files to change. For a website, prefer plain "
        "HTML, CSS and JavaScript with an index.html at the top of this directory and no "
        "build step, unless the request asks for something else. Make the last step check "
        "that the whole result works. If the request is unclear, ask before writing the plan."
    )


def step_title(index: int, total: int, step: PlanStep) -> str:
    return _short(f"{index}/{total} {step.title}", 80)


def step_prompt(request: str, plan: Plan, index: int) -> str:
    """The prompt of step ``index`` (1-based): the step, with the project around it."""
    if plan.parallel:
        return _parallel_step_prompt(request, plan, index)
    step = plan.steps[index - 1]
    outline = "; ".join(f"{n}. {s.title}" for n, s in enumerate(plan.steps, start=1))
    done = f"Steps 1 to {index - 1} are done. " if index > 2 else "Step 1 is done. "
    return one_line(
        "You are doing one step of a project in this directory. "
        f"The request was: {request} The plan: {plan.summary} Its steps: {outline}. "
        f"{done if index > 1 else ''}Do step {index} now, and only this step. "
        f"Step {index}, {step.title}: {step.instructions}"
    )


def _parallel_step_prompt(request: str, plan: Plan, index: int) -> str:
    step = plan.steps[index - 1]
    outline = "; ".join(f"{n}. {s.title}" for n, s in enumerate(plan.steps, start=1))
    needs = plan.needs(index)
    many = len(needs) > 1
    done = (
        f"Step{'s' if many else ''} {_numbers(needs)} {'are' if many else 'is'} done, and "
        "the work is in this directory. "
        if needs
        else ""
    )
    alongside = plan.alongside(index)
    others = (
        f"At the same time other agents are doing step{'s' if len(alongside) > 1 else ''} "
        f"{_numbers(alongside)}, each in its own copy of this directory; their work and "
        "yours are merged afterwards, so change only what this step needs and leave their "
        "parts to them. "
        if alongside
        else ""
    )
    return one_line(
        "You are doing one step of a project in this directory. "
        f"The request was: {request} The plan: {plan.summary} Its steps: {outline}. "
        f"{done}{others}Do step {index} now, and only this step. "
        f"Step {index}, {step.title}: {step.instructions}"
    )


def merge_prompt(branch: str, title: str, files: tuple[str, ...]) -> str:
    """The prompt of a merge task: take in a step whose branch conflicted (ADR 0017)."""
    where = ", ".join(files[:20]) + (" and more" if len(files) > 20 else "")
    return one_line(
        "This directory is a project that several agents worked on at the same time, each "
        f"on a branch of its own. Merge the branch {branch} (the step {title}) into the "
        f"current branch: run git merge {branch}. It conflicts in {where}. Resolve every "
        "conflict so that the work of both sides is kept, then git add the files and "
        "git commit --no-edit. Change nothing else, and check that the result still works."
    )


def _numbers(numbers: tuple[int, ...]) -> str:
    if len(numbers) == 1:
        return str(numbers[0])
    return ", ".join(str(n) for n in numbers[:-1]) + f" and {numbers[-1]}"


def read_plan(text: str) -> Plan:
    """The plan in ``text`` (the content of the plan file)."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as err:
        msg = f"{PLAN_FILE} is not JSON: {err.msg} at line {err.lineno}"
        raise PlanError(msg) from err
    try:
        return Plan.model_validate(data)
    except ValidationError as err:
        first = err.errors()[0]
        where = ".".join(str(part) for part in first["loc"]) or "the file"
        msg = f"{PLAN_FILE}: {where}: {first['msg']}"
        raise PlanError(msg) from err


def _short(text: str, limit: int) -> str:
    text = one_line(text)
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
