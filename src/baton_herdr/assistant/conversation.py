"""The assistant's side of a conversation (ADR 0021): one message in, one answer out.

The model interprets; baton acts. An answer that does not fit the schema is small talk
saying it was not understood. A status question is answered from the log's digest: in a
fixed form, or, for a "why", explained by the model from the digest alone, with the events
it relied on cited and any citation the digest does not hold dropped.

One message is answered at a time, within a time limit; a message that arrives meanwhile
is told to wait, not queued, so the bot never stalls behind the model.
"""

from __future__ import annotations

import asyncio
import re
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from baton_herdr.assistant.cards import PREFIX as CARD_PREFIX
from baton_herdr.assistant.cards import Card, CardState, CardStore
from baton_herdr.assistant.digest import brief, build_digest
from baton_herdr.core.assistant import (
    MODEL_SCHEMA,
    AssistantTurn,
    AssistantUnavailableError,
    ChatMessage,
    Interpretation,
    StatusQuery,
    model_schema,
    parse_intent,
)
from baton_herdr.core.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from datetime import tzinfo

    from baton_herdr.assistant.digest import Digest
    from baton_herdr.core.assistant import AssistantBackend
    from baton_herdr.core.ports import EventStore

type Language = Literal["tr", "en"]

INTENT_INSTRUCTIONS = """\
You are the assistant of baton, a tool that runs coding agents (Claude Code, Codex) on
tasks (ids like t-1a2b3c4d) and projects (ids like p-1a2b3c). You only interpret the
operator's message; you cannot run anything. Classify it:
- "status": a question about tasks, projects or agents. Put the ids it names in
  status.ids (none: everything open). status.why = true when it asks why (why stuck, why
  moved, why failed).
- "new_project": a request to build or change something. new_project.request is the
  request, complete, as the operator would write it to an agent. target.kind is "new".
  When something essential is missing, ask it in new_project.question.
- "chat": anything else. Answer in reply.
confidence is how sure you are of the intent, from 0 to 1. reply is at most two short
sentences. Fill status for a status intent and new_project for a new_project intent, never
null for its own intent; leave the other one null.
{language}
Answer with one JSON object only:
{{"intent": "chat" | "status" | "new_project", "reply": "...", "confidence": 0.9,
 "status": {{"ids": ["t-1a2b3c4d"], "why": false}} or null,
 "new_project": {{"request": "...", "target": {{"kind": "new"}}, "question": "..." or null}}
 or null}}"""

EXPLAIN_INSTRUCTIONS = """\
You explain to the operator what baton's log says about their question. Use only the
status digest you are given. For anything the digest does not hold, say it is not in the
record; never guess. In cites, list the digest entries you relied on, exactly as they
appear there (#12, a-1a2b3c4d). answer is at most three short sentences.
{language}
Answer with one JSON object only: {{"answer": "...", "cites": ["#12", "a-1a2b3c4d"]}}"""

_LANGUAGE = {
    "auto": "Write reply in the language of the operator's message.",
    "tr": "Write reply in Turkish.",
    "en": "Write reply in English.",
}

TEXTS: dict[str, dict[Language, str]] = {
    "busy": {
        "tr": "Bir önceki mesajını düşünüyorum; birazdan yeniden sor.",
        "en": "Still thinking about your last message; ask again in a moment.",
    },
    "unavailable": {
        "tr": "Asistan şu an yanıt veremiyor. /status ve /new yine çalışıyor.",
        "en": "The assistant did not answer. /status and /new still work.",
    },
    "not_understood": {
        "tr": "Anlayamadım; /new ya da /status ile dener misin?",
        "en": "I did not understand; try /new or /status.",
    },
    "unknown": {"tr": "{ids}: kayıtta böyle bir iş yok.", "en": "{ids}: no such task in the log."},
    "nothing_open": {"tr": "Açık iş yok.", "en": "No open tasks."},
    "sources": {"tr": "Kaynak", "en": "Sources"},
    "existing_later": {
        "tr": "Var olan bir klasörde iş başlatmak henüz yok; şimdilik: baton task add",
        "en": "Work in an existing directory is not here yet; for now: baton task add",
    },
    "card": {
        "tr": "📝 Proje önerisi\n{request}\n\n"
        "Nerede: {where}/… (klasörü baton açar)\n"
        "Ajan (şu an): {agent}\n"
        "Katman: {tier}\n"
        "Önce bir ajan planlar; plan onayına gelir, onaylamadan kod yazılmaz.\n"
        "Kart {minutes} dakika geçerli.",
        "en": "📝 Project proposal\n{request}\n\n"
        "Where: {where}/… (baton makes the directory)\n"
        "Agent (now): {agent}\n"
        "Tier: {tier}\n"
        "An agent plans it first; the plan comes to you, and nothing is coded before you "
        "start it.\n"
        "This card can be pressed for {minutes} minutes.",
    },
    "edit": {
        "tr": "Neyi değiştireyim? Bir sonraki mesajını değişiklik olarak alacağım.",
        "en": "What should change? I will take your next message as the change.",
    },
    "cancelled": {
        "tr": "İptal edildi; hiçbir şey başlatılmadı.",
        "en": "Cancelled; nothing was started.",
    },
    "expired": {
        "tr": "Bu kartın süresi dolmuş; isteği yeniden yaz.",
        "en": "This card has expired; send the request again.",
    },
    "already_started": {"tr": "Zaten başlatıldı. {result}", "en": "Already started. {result}"},
    "starting": {"tr": "Başlatılıyor…", "en": "Starting…"},
    "was_cancelled": {"tr": "Bu kart iptal edilmişti.", "en": "This card was cancelled."},
    "superseded": {
        "tr": "Bu kartın yerine yenisi geldi.",
        "en": "A newer card replaced this one.",
    },
    "buttons": {"tr": "Başlat|Düzenle|İptal", "en": "Start|Edit|Cancel"},
}

# Letters only Turkish writes, among the languages the bot speaks, and common Turkish
# words written without them: enough to pick tr or en for baton's own sentences when the
# language is "auto". Case-sensitive for the letters: under IGNORECASE "İ" matches "i".
_TURKISH_LETTERS = re.compile(r"[çğıöşüÇĞİÖŞÜ]")
_TURKISH_WORDS = frozenset(
    {
        "neden",
        "nerede",
        "nedir",
        "ne",
        "mi",
        "mu",
        "mü",
        "var",
        "yok",
        "hangi",
        "kaç",
        "merhaba",
        "selam",
        "lütfen",
        "durum",
        "bir",
        "için",
        "ve",
        "ile",
        "bu",
        "şu",
        "iş",
        "işi",
        "proje",
        "yap",
        "yaz",
    }
)


class Explanation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    answer: str = Field(min_length=1, max_length=1500)
    cites: tuple[str, ...] = Field(default=(), max_length=20)


EXPLANATION_SCHEMA = model_schema(Explanation)


@dataclass(frozen=True, slots=True)
class Reply:
    """What the operator is sent: text, and the buttons of a card when there is one."""

    text: str
    buttons: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class CardContext:
    """What a card says besides the request, from the bot: where the project would be made,
    the agent a task would go to now and why, and the tier its plan runs in."""

    where: str
    agent: str
    tier: str


type CardContextFor = Callable[[str], Awaitable[CardContext]]
type StartProject = Callable[[str], Awaitable[tuple[bool, str]]]


@dataclass(frozen=True, slots=True)
class AssistantConfig:
    timeout_s: float
    min_confidence: float
    history_messages: int
    language: Literal["auto", "tr", "en"]
    zone: tzinfo


class Assistant:
    def __init__(
        self,
        *,
        backend: AssistantBackend,
        store: EventStore,
        config: AssistantConfig,
        cards: CardStore | None = None,
    ) -> None:
        self._backend = backend
        self._store = store
        self._config = config
        # Proposed projects waiting for a press (A3); None: the bot shows no cards.
        self._cards = cards
        self._busy = asyncio.Lock()
        self._history: deque[ChatMessage] = deque(maxlen=config.history_messages)
        self._log = get_logger("baton.assistant")

    async def answer(self, message: str, context: CardContextFor | None = None) -> Reply:
        """The answer to one plain message from the operator. ``context`` fills a card in,
        when the message proposes a project."""
        language = self._language(message)
        if self._busy.locked():
            return Reply(TEXTS["busy"][language])
        async with self._busy:
            try:
                reply = await self._answer(message, language, context)
            except (TimeoutError, AssistantUnavailableError) as err:
                self._log.warning("assistant unavailable", error=str(err) or "timed out")
                return Reply(TEXTS["unavailable"][language])
            self._history.extend(
                (ChatMessage("operator", message), ChatMessage("assistant", reply.text))
            )
            return reply

    async def press(self, data: str, start: StartProject) -> str:
        """A press on a card's button (``as:<action>:<card>``). ``start`` makes the project
        the way /new does; it runs at most once per card."""
        _, action, card_id = [*data.split(":", 2), "", ""][:3]
        if self._cards is None:
            return TEXTS["expired"]["en"]
        pressed = await self._cards.press(card_id, action, start)
        card = pressed.card
        if card is None:
            return TEXTS["expired"]["en"]
        language: Language = "tr" if card.language == "tr" else "en"
        self._log.info("assistant card", card=card.card_id, action=action, state=card.state)
        return _press_text(card, acted=pressed.acted, language=language)

    async def _answer(
        self, message: str, language: Language, context: CardContextFor | None
    ) -> Reply:
        editing = self._cards.editing() if self._cards is not None else None
        if (known := plain_status(message)) is not None:
            # The intent is plain without the model: a call saved, the slowest part.
            self._log.info("assistant intent", intent="status", why=known.why, model=False)
            interpretation = Interpretation(intent="status", reply="", confidence=1.0, status=known)
            return Reply(await self._act(interpretation, message, language))
        if editing is not None:
            # The operator pressed Edit: this message is the change to that request.
            message = EDIT_FRAME.format(request=editing.request, change=message)
        raw = await self._ask(
            AssistantTurn(
                instructions=self._instructions(INTENT_INSTRUCTIONS),
                message=message,
                schema=MODEL_SCHEMA,
                history=tuple(self._history),
            )
        )
        parsed = parse_intent(raw, min_confidence=self._config.min_confidence)
        self._log.info(
            "assistant intent", intent=parsed.interpretation.intent, rejected=parsed.rejected
        )
        if parsed.rejected is not None:
            return Reply(TEXTS["not_understood"][language])
        proposal = parsed.interpretation.new_project
        if proposal is not None and not proposal.question and self._cards is not None:
            if proposal.target.kind != "new":
                return Reply(TEXTS["existing_later"][language])
            if editing is not None:
                self._cards.supersede(editing.card_id)
            return await self._card(proposal.request, language, context)
        return Reply(await self._act(parsed.interpretation, message, language))

    async def _card(
        self, request: str, language: Language, context: CardContextFor | None
    ) -> Reply:
        assert self._cards is not None  # noqa: S101 - the caller checked
        card = self._cards.propose(request, language=language)
        about = await context(request) if context is not None else CardContext("-", "-", "-")
        minutes = int(self._cards.ttl.total_seconds() // 60)
        text = TEXTS["card"][language].format(
            request=request, where=about.where, agent=about.agent, tier=about.tier, minutes=minutes
        )
        labels = TEXTS["buttons"][language].split("|")
        actions = ("start", "edit", "cancel")
        buttons = tuple(
            (label, f"{CARD_PREFIX}{action}:{card.card_id}")
            for label, action in zip(labels, actions, strict=True)
        )
        return Reply(text, buttons)

    async def _act(self, interpretation: Interpretation, message: str, language: Language) -> str:
        if interpretation.status is not None:
            digest = build_digest(
                await self._store.read(), ids=interpretation.status.ids, zone=self._config.zone
            )
            if interpretation.status.why and digest.tasks:
                return await self._explain(message, digest, language)
            return _brief(digest, language)
        if interpretation.new_project is not None:
            proposal = interpretation.new_project
            if proposal.question:
                return proposal.question
            return f"/new {proposal.request}"  # no cards: the command that would do it
        return interpretation.reply or TEXTS["not_understood"][language]

    async def _explain(self, message: str, digest: Digest, language: Language) -> str:
        """A "why", answered by the model from the digest alone; citations it makes that
        the digest does not hold are dropped."""
        raw = await self._ask(
            AssistantTurn(
                instructions=self._instructions(EXPLAIN_INSTRUCTIONS),
                message=message,
                schema=EXPLANATION_SCHEMA,
                digest=digest.text,
            )
        )
        try:
            explanation = Explanation.model_validate_json(raw)
        except ValidationError:
            self._log.debug("assistant explanation rejected", raw=raw[:2000])
            return _brief(digest, language)  # the facts, without the model's words
        cites = [c for c in dict.fromkeys(explanation.cites) if c in digest.cites]
        dropped = set(explanation.cites) - set(cites)
        if dropped:
            self._log.info("assistant citations dropped", cites=sorted(dropped))
        lines = [explanation.answer]
        if cites:
            lines.append(f"{TEXTS['sources'][language]}: {', '.join(cites)}")
        if digest.unknown:
            lines.append(TEXTS["unknown"][language].format(ids=", ".join(digest.unknown)))
        return "\n\n".join(lines)

    async def _ask(self, turn: AssistantTurn) -> str:
        return await asyncio.wait_for(self._backend.interpret(turn), self._config.timeout_s)

    def _instructions(self, template: str) -> str:
        # The shape of the answer is written out in words; the schema itself goes to the
        # backend as the grammar of the answer (Ollama's format). Writing the schema into
        # the prompt as well doubled it, and on a CPU the prompt is most of the wait.
        return template.format(language=_LANGUAGE[self._config.language])

    def _language(self, message: str) -> Language:
        if self._config.language != "auto":
            return self._config.language
        words = set(re.findall(r"\w+", message.lower()))
        turkish = _TURKISH_LETTERS.search(message) or words & _TURKISH_WORDS
        return "tr" if turkish else "en"


_IDS = re.compile(r"\b(t-[0-9a-f]{8}|p-[0-9a-f]{6})\b")
_WHY = frozenset({"neden", "niye", "niçin", "why"})
_STATUS = frozenset(
    {
        "durum",
        "durumda",
        "durumu",
        "aşamada",
        "nerede",
        "bitti",
        "status",
        "state",
        "progress",
        "done",
    }
)
# A short question about everything open, naming no task: "açık işler neler?", "what is
# running?". Seen live: the model read it as small talk one time in two.
_OPEN = re.compile(
    r"^\s*(açık|çalışan|bekleyen|ne)\s+(işler|iş|görevler|var|çalışıyor)\b"
    r"|^\s*(what'?s|what is|what are)\s+(running|open|going on)\b",
    re.IGNORECASE,
)


def plain_status(message: str) -> StatusQuery | None:
    """A status question that needs no model: it names tasks or projects and asks how they
    stand or why, in a word of its own, and asks nothing to be built. Anything else is the
    model's to read."""
    ids = tuple(dict.fromkeys(_IDS.findall(message)))
    words = set(re.findall(r"\w+", message.lower()))
    if words & _BUILD or len(ids) > MAX_IDS:
        return None
    if not ids:
        return StatusQuery() if _OPEN.match(message) and len(words) <= MAX_OPEN_WORDS else None
    if words & _WHY:
        return StatusQuery(ids=ids, why=True)
    if words & _STATUS or message.strip().rstrip("?") in ids:
        return StatusQuery(ids=ids)
    return None


# Words that ask for work, not for news: such a message goes to the model.
_BUILD = frozenset(
    {"yap", "yaz", "ekle", "düzelt", "oluştur", "build", "make", "add", "fix", "write"}
)
MAX_IDS = 5
MAX_OPEN_WORDS = 6


# An edit, put to the model as one message: the request as it stood, and what changes.
EDIT_FRAME = (
    "Revise this project request and propose it again as new_project.\n"
    "Request: {request}\nChange: {change}"
)


def _press_text(card: Card, *, acted: bool, language: Language) -> str:  # noqa: PLR0911 - one per state
    """What a press is answered with: what it did, or the state that kept it from acting."""
    match card.state:
        case CardState.STARTED:
            return (
                card.result
                if acted
                else TEXTS["already_started"][language].format(result=card.result)
            )
        case CardState.FAILED:
            return card.result
        case CardState.EDITING:
            return TEXTS["edit"][language]
        case CardState.CANCELLED:
            return TEXTS["cancelled" if acted else "was_cancelled"][language]
        case CardState.STARTING:
            return TEXTS["starting"][language]
        case CardState.SUPERSEDED:
            return TEXTS["superseded"][language]
        case _:
            return TEXTS["expired"][language]


def _brief(digest: Digest, language: Language) -> str:
    lines = brief(digest) or ([] if digest.unknown else [TEXTS["nothing_open"][language]])
    if digest.unknown:
        lines.append(TEXTS["unknown"][language].format(ids=", ".join(digest.unknown)))
    return "\n".join(lines)
