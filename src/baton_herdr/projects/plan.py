"""What the agents are asked, and reading the plan back (ADR 0016). Pure.

A project starts with a plan task: the agent is asked to write ``.baton/plan.json`` and
nothing else. Each approved step then becomes a task whose prompt carries the request
and the plan around the step, because the agent of a step has seen neither.
"""

from __future__ import annotations

import json

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from baton_herdr.core.contract import one_line
from baton_herdr.core.events import PlanStep

PLAN_FILE = ".baton/plan.json"
MAX_STEPS = 8


class Plan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    summary: str = Field(default="", max_length=2000)
    steps: tuple[PlanStep, ...] = Field(min_length=1, max_length=MAX_STEPS)


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
        '"steps": [{"title": "a short name", "instructions": "what to do in this step"}]}. '
        f"Use 1 to {MAX_STEPS} steps, in the order they must be done. Each step is done "
        "later by another agent who sees only the request, the summary, the list of step "
        "titles and that step's instructions, so make the instructions complete. Each step "
        "should leave something that can be checked on its own. For a website, prefer plain "
        "HTML, CSS and JavaScript with an index.html at the top of this directory and no "
        "build step, unless the request asks for something else. Make the last step check "
        "that the whole result works. If the request is unclear, ask before writing the plan."
    )


def step_title(index: int, total: int, step: PlanStep) -> str:
    return _short(f"{index}/{total} {step.title}", 80)


def step_prompt(request: str, plan: Plan, index: int) -> str:
    """The prompt of step ``index`` (1-based): the step, with the project around it."""
    step = plan.steps[index - 1]
    outline = "; ".join(f"{n}. {s.title}" for n, s in enumerate(plan.steps, start=1))
    done = f"Steps 1 to {index - 1} are done. " if index > 2 else "Step 1 is done. "
    return one_line(
        "You are doing one step of a project in this directory. "
        f"The request was: {request} The plan: {plan.summary} Its steps: {outline}. "
        f"{done if index > 1 else ''}Do step {index} now, and only this step. "
        f"Step {index}, {step.title}: {step.instructions}"
    )


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
