"""The natural-language assistant's contract (ADR 0021). Pure.

A model only interprets the operator's message: it has no file system, shell or herdr.
Its answer is text that must fit :data:`INTENT_SCHEMA`; :func:`parse_intent` holds every
backend to it, and whatever does not fit becomes ``chat``, so nothing acts on it. Work is
started only through the existing functions, after the operator presses a button.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from baton_herdr.core.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Mapping

# Said when the model's answer cannot be used; the operator still has the commands.
NOT_UNDERSTOOD = "I did not understand; try /new or /status."
# A task or project id, as baton makes them (t-1a2b3c4d, p-1a2b3c).
TASK_OR_PROJECT_ID = r"^(t-[0-9a-f]{8}|p-[0-9a-f]{6})$"

_log = get_logger("baton.assistant")


class AssistantUnavailableError(Exception):
    """The backend could not answer: not reachable, too slow, or refused."""


@dataclass(frozen=True, slots=True)
class AssistantHealth:
    up: bool
    detail: str


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: Literal["operator", "assistant"]
    text: str


@dataclass(frozen=True, slots=True)
class AssistantTurn:
    """What a backend gets for one answer.

    ``message`` is the operator's text, to be given to the model as data to classify, never
    as instructions. ``schema`` is the JSON Schema the answer must fit. ``digest`` is the
    status summary baton built from the log, when one was asked for.
    """

    instructions: str
    message: str
    schema: Mapping[str, Any]
    history: tuple[ChatMessage, ...] = ()
    digest: str | None = None


class AssistantBackend(Protocol):
    @property
    def name(self) -> str: ...

    async def interpret(self, turn: AssistantTurn) -> str:
        """The model's raw answer to ``turn``, expected to fit ``turn.schema``. Raises
        :class:`AssistantUnavailableError` when there is none."""
        ...

    async def health(self) -> AssistantHealth:
        """Whether the backend can answer now, and the model it would use."""
        ...


class _Schema(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Target(_Schema):
    """Where a project's work goes. v1 takes only ``new``: a directory baton makes. An
    ``existing`` directory is in the schema already, for v2, so it does not change then."""

    kind: Literal["new", "existing"]
    path: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _path_for_existing(self) -> Target:
        if (self.kind == "existing") != (self.path is not None):
            msg = "an existing target names a path, and only it does"
            raise ValueError(msg)
        return self


class StatusQuery(_Schema):
    # Tasks or projects asked about; empty: everything open.
    ids: tuple[Annotated[str, Field(pattern=TASK_OR_PROJECT_ID)], ...] = Field(
        default=(), max_length=5
    )
    # "Why is it stuck?", "why did it move?": answered by the model from the digest.
    why: bool = False


class ProjectProposal(_Schema):
    # The request as it would be passed to /new.
    request: str = Field(min_length=10, max_length=2000)
    target: Target
    # Set when the request is not complete enough: asked instead of showing a card.
    question: str | None = Field(default=None, max_length=300)


class Interpretation(_Schema):
    intent: Literal["chat", "status", "new_project"]
    # What the operator is told: the chat answer, or the question to ask.
    reply: str = Field(max_length=1500)
    confidence: float = Field(ge=0, le=1)
    status: StatusQuery | None = None
    new_project: ProjectProposal | None = None

    @model_validator(mode="after")
    def _fits_the_intent(self) -> Interpretation:
        if (self.intent == "status") != (self.status is not None):
            msg = "a status intent carries status, and only it does"
            raise ValueError(msg)
        if (self.intent == "new_project") != (self.new_project is not None):
            msg = "a new_project intent carries new_project, and only it does"
            raise ValueError(msg)
        return self


# The full contract, which parse_intent enforces.
INTENT_SCHEMA: Mapping[str, Any] = Interpretation.model_json_schema()


def _without(schema: Any, keys: frozenset[str]) -> Any:
    if isinstance(schema, dict):
        return {k: _without(v, keys) for k, v in schema.items() if k not in keys}
    if isinstance(schema, list):
        return [_without(item, keys) for item in schema]
    return schema


# What the model is given as the shape of its answer (Ollama's ``format``), and shown in
# its instructions, as Ollama advises. Without ``maxLength``: Ollama 0.34 cannot turn a
# long one (2000 characters) into a grammar and refuses the request. The lengths still
# hold, as parse_intent checks the answer against INTENT_SCHEMA.
MODEL_SCHEMA: Mapping[str, Any] = _without(INTENT_SCHEMA, frozenset({"maxLength"}))


def not_understood() -> Interpretation:
    return Interpretation(intent="chat", reply=NOT_UNDERSTOOD, confidence=0.0)


@dataclass(frozen=True, slots=True)
class Parsed:
    """An interpretation, and why the model's own one was not used, if it was not."""

    interpretation: Interpretation
    rejected: str | None = None
    raw: str = field(default="", repr=False)


def parse_intent(raw: str, *, min_confidence: float) -> Parsed:
    """The model's answer as an :class:`Interpretation`, or ``chat`` saying it was not
    understood: for text that is not JSON, does not fit the schema, or fits it with less
    confidence than ``min_confidence``. The raw text is logged at debug level only."""
    try:
        interpretation = Interpretation.model_validate_json(_strip_fence(raw))
    except (ValidationError, ValueError) as err:
        reason = _first_error(err)
        _log.debug("assistant answer rejected", reason=reason, raw=raw[:2000])
        return Parsed(not_understood(), rejected=reason, raw=raw)
    if interpretation.intent != "chat" and interpretation.confidence < min_confidence:
        reason = f"confidence {interpretation.confidence:.2f} below {min_confidence:.2f}"
        _log.debug("assistant answer rejected", reason=reason, raw=raw[:2000])
        return Parsed(not_understood(), rejected=reason, raw=raw)
    return Parsed(interpretation, raw=raw)


def schema_text(schema: Mapping[str, Any]) -> str:
    """The schema as it is written into the instructions."""
    return json.dumps(schema, separators=(",", ":"), sort_keys=True)


def _strip_fence(raw: str) -> str:
    """Some models wrap JSON in a Markdown fence even when asked not to."""
    match = re.fullmatch(r"\s*```(?:json)?\s*(.*?)\s*```\s*", raw, flags=re.DOTALL)
    return match.group(1) if match else raw


def _first_error(err: Exception) -> str:
    if isinstance(err, ValidationError) and err.errors():
        first = err.errors()[0]
        where = ".".join(str(p) for p in first["loc"]) or "answer"
        return f"{where}: {first['msg']}"
    return str(err)[:200]
