"""The assistant's contract: what the model may answer, and what becomes of the rest
(ADR 0021)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from baton_herdr.core.assistant import (
    INTENT_SCHEMA,
    MODEL_SCHEMA,
    NOT_UNDERSTOOD,
    AssistantTurn,
    AssistantUnavailableError,
    parse_intent,
    schema_text,
)
from baton_herdr.core.config import AssistantSettings, BatonSettings
from baton_herdr.core.fakes import FakeAssistant


def answer(**fields: Any) -> str:
    return json.dumps({"reply": "", "confidence": 0.9, **fields})


def parsed(raw: str, *, min_confidence: float = 0.5) -> tuple[str, str | None]:
    result = parse_intent(raw, min_confidence=min_confidence)
    return result.interpretation.intent, result.rejected


def test_a_status_question_is_read_with_its_ids() -> None:
    raw = answer(intent="status", status={"ids": ["t-1a2b3c4d"], "why": True})
    interpretation = parse_intent(raw, min_confidence=0.5).interpretation
    assert interpretation.intent == "status"
    assert interpretation.status is not None
    assert interpretation.status.ids == ("t-1a2b3c4d",)
    assert interpretation.status.why


def test_a_project_proposal_names_its_target() -> None:
    raw = answer(
        intent="new_project",
        new_project={"request": "A one-page site for my cafe", "target": {"kind": "new"}},
    )
    proposal = parse_intent(raw, min_confidence=0.5).interpretation.new_project
    assert proposal is not None
    assert (proposal.request, proposal.target.kind, proposal.question) == (
        "A one-page site for my cafe",
        "new",
        None,
    )


def test_an_existing_target_fits_the_schema_already_for_v2() -> None:
    target = {"kind": "existing", "path": "/home/me/src/calc"}
    raw = answer(
        intent="new_project", new_project={"request": "Add a power function", "target": target}
    )
    assert parsed(raw) == ("new_project", None)
    # A path only with an existing target, and always with one.
    no_path = answer(
        intent="new_project",
        new_project={"request": "Add a power function", "target": {"kind": "existing"}},
    )
    assert parsed(no_path)[0] == "chat"


def test_small_talk_is_chat() -> None:
    raw = answer(intent="chat", reply="Merhaba! Nasıl yardımcı olabilirim?")
    result = parse_intent(raw, min_confidence=0.5)
    assert result.interpretation.reply == "Merhaba! Nasıl yardımcı olabilirim?"
    assert result.rejected is None


@pytest.mark.parametrize(
    ("raw", "why"),
    [
        ("Sure! Here is the status.", "not JSON"),
        ("{", "cut short"),
        (json.dumps({"intent": "status", "confidence": 0.9}), "a field is missing"),
        (answer(intent="cancel"), "an intent v1 does not have"),
        (answer(intent="status"), "a status intent without its status"),
        (
            answer(intent="chat", status={"ids": []}),
            "a chat intent carrying a status",
        ),
        (
            answer(intent="status", status={"ids": ["../../etc/passwd"]}),
            "an id that is not one",
        ),
        (
            answer(intent="new_project", new_project={"request": "x", "target": {"kind": "new"}}),
            "a request too short to start anything",
        ),
        (answer(intent="chat", run="rm -rf ~"), "a field the schema does not have"),
    ],
)
def test_an_answer_that_does_not_fit_is_chat_saying_so(raw: str, why: str) -> None:
    result = parse_intent(raw, min_confidence=0.5)
    assert result.interpretation.intent == "chat", why
    assert result.interpretation.reply == NOT_UNDERSTOOD
    assert result.rejected


def test_an_unsure_proposal_is_chat_but_unsure_small_talk_stays() -> None:
    unsure = answer(intent="status", status={}, confidence=0.3)
    assert parsed(unsure) == ("chat", "confidence 0.30 below 0.50")
    assert parsed(answer(intent="chat", reply="hi", confidence=0.1)) == ("chat", None)


def test_a_fenced_answer_is_read() -> None:
    raw = "```json\n" + answer(intent="status", status={}) + "\n```"
    assert parsed(raw) == ("status", None)


def test_the_model_is_given_the_schema_without_lengths_ollama_cannot_compile() -> None:
    assert "maxLength" in json.dumps(INTENT_SCHEMA)
    assert "maxLength" not in json.dumps(MODEL_SCHEMA)
    assert MODEL_SCHEMA["required"] == INTENT_SCHEMA["required"]
    # An answer longer than the contract allows is still refused.
    too_long = answer(intent="chat", reply="x" * 1501)
    assert parse_intent(too_long, min_confidence=0.5).rejected


def test_the_schema_is_plain_json_and_names_every_intent() -> None:
    text = schema_text(INTENT_SCHEMA)
    assert json.loads(text) == INTENT_SCHEMA
    intents = INTENT_SCHEMA["properties"]["intent"]["enum"]
    assert intents == ["chat", "status", "new_project"]
    assert set(INTENT_SCHEMA["required"]) == {"intent", "reply", "confidence"}


async def test_the_fake_gives_its_answers_in_turn_then_is_unavailable() -> None:
    fake = FakeAssistant("one", RuntimeError("boom"))
    turn = AssistantTurn(instructions="-", message="hi", schema=INTENT_SCHEMA)
    assert await fake.interpret(turn) == "one"
    with pytest.raises(RuntimeError, match="boom"):
        await fake.interpret(turn)
    with pytest.raises(AssistantUnavailableError):
        await fake.interpret(turn)
    assert [t.message for t in fake.turns] == ["hi", "hi", "hi"]
    assert (await fake.health()).up


def test_the_assistant_is_off_unless_a_backend_and_model_are_set() -> None:
    assert not BatonSettings().assistant.enabled
    on = AssistantSettings(backend="ollama", model="ornith-1.5:9b")
    assert on.enabled
    assert (on.url, on.language, on.keep_alive) == ("http://127.0.0.1:11434", "auto", "5m")
    with pytest.raises(ValidationError, match="needs a model"):
        AssistantSettings(backend="ollama")
    with pytest.raises(ValidationError):
        AssistantSettings.model_validate({"language": "de"})
