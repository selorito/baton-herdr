"""Read ``policy.yaml``: the user's rules, which come before the defaults (ADR 0013).

```yaml
rules:
  - decision: allow          # allow | ask | deny
    command: "make lint*"    # a shell-style pattern over one command
    reason: our linter       # optional; goes into the event log and notices
  - decision: deny
    regex: "^terraform\\s+(apply|destroy)"   # or a regular expression
    agents: [codex]          # optional; every agent by default
```

A missing file means the defaults alone. A file that cannot be read is an error: batond
does not start with a policy it only half understood.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from baton_herdr.core.model import AgentKind
from baton_herdr.policy.defaults import DEFAULT_RULES
from baton_herdr.policy.rules import Decision, Rule, Verdict, decide

if TYPE_CHECKING:
    from pathlib import Path


class PolicyError(Exception):
    """The policy file exists but is not a valid policy; the message says where."""


class _RuleSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: Decision
    command: str | None = Field(default=None, min_length=1)
    regex: str | None = Field(default=None, min_length=1)
    agents: tuple[AgentKind, ...] | None = Field(default=None, min_length=1)
    reason: str | None = None

    @model_validator(mode="after")
    def _one_pattern(self) -> Self:
        if (self.command is None) == (self.regex is None):
            raise ValueError("give either command or regex")
        if self.regex is not None:
            try:
                re.compile(self.regex)
            except re.error as err:
                raise ValueError(f"regex does not compile: {err}") from err
        return self


class _FileSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rules: tuple[_RuleSpec, ...] = ()


@dataclass(frozen=True, slots=True)
class Policy:
    """The rules in the order they are tried: the user's, then the defaults."""

    rules: tuple[Rule, ...] = DEFAULT_RULES
    source: str = "defaults only"

    def decide(self, agent: AgentKind, command: str | None) -> Verdict:
        return decide(self.rules, agent, command)


def load_policy(path: Path) -> Policy:
    """The policy in ``path`` followed by the defaults; the defaults alone if it is missing."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Policy()
    except OSError as err:
        raise PolicyError(f"{path}: {err.strerror}") from err
    try:
        spec = _FileSpec.model_validate(yaml.safe_load(text) or {})
    except yaml.YAMLError as err:
        raise PolicyError(f"{path}: not valid YAML: {err}") from err
    except ValidationError as err:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'file'}: {e['msg']}" for e in err.errors()
        )
        raise PolicyError(f"{path}: {problems}") from err
    own = tuple(
        Rule(
            decision=rule.decision,
            pattern=rule.regex or rule.command or "",
            reason=rule.reason or f"rule {n} of policy.yaml",
            regex=rule.regex is not None,
            agents=frozenset(rule.agents) if rule.agents else None,
            origin=f"policy.yaml rule {n}",
        )
        for n, rule in enumerate(spec.rules, start=1)
    )
    return Policy(rules=own + DEFAULT_RULES, source=str(path))
