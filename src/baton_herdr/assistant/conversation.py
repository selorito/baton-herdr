"""The assistant's side of a conversation (ADR 0021): one message in, one answer out.

The model interprets; baton acts. An answer that does not fit the schema is small talk
saying it was not understood. A project, a cancel or a change of tier comes as a card,
done only when the operator presses its button: a new project, or a task in a directory
that exists once baton has checked that the directory may take work (``places``). Which
task a cancel or a tier is for is found by baton, and asked when it is not plain. A
status question is answered from the log's digest: in a fixed form, or, for a "why",
explained by the model from the digest alone, with the events it relied on cited and any
citation the digest does not hold dropped.

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
from baton_herdr.assistant.cards import Card, CardKind, CardState, CardStore
from baton_herdr.assistant.digest import brief, build_digest
from baton_herdr.assistant.places import Place, Places, Refused
from baton_herdr.assistant.targets import (
    MAX_CHOICES,
    Ambiguous,
    Closed,
    OneProject,
    OneTask,
    Unknown,
    find_target,
)
from baton_herdr.core.assistant import (
    MODEL_SCHEMA,
    AssistantTurn,
    AssistantUnavailableError,
    ChatMessage,
    Interpretation,
    StatusQuery,
    TaskRef,
    model_schema,
    parse_intent,
)
from baton_herdr.core.logging import get_logger
from baton_herdr.core.projection import project

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from datetime import tzinfo

    from baton_herdr.assistant.digest import Digest
    from baton_herdr.assistant.targets import Found
    from baton_herdr.core.assistant import AssistantBackend
    from baton_herdr.core.ports import EventStore
    from baton_herdr.core.projection import TaskView

type Language = Literal["tr", "en"]

INTENT_INSTRUCTIONS = """\
You are the assistant of baton, a tool that runs coding agents (Claude Code, Codex) on
tasks (ids like t-1a2b3c4d) and projects (ids like p-1a2b3c). You only interpret the
operator's message; you cannot run anything. Classify it:
- "status": a question about tasks, projects or agents. Put the ids it names in
  status.ids (none: everything open). status.why = true when it asks why (why stuck, why
  moved, why failed).
- "new_project": a request to build or change something. new_project.request is the
  request, complete, as the operator would write it to an agent. target.kind is "new" for
  something new. When the message names a directory or a project (p-1a2b3c) to work in,
  target is {{"kind": "existing", "path": ...}} with that directory exactly as written
  (~/src/calc, /home/me/calc) or that project id. When something essential is missing,
  ask it in new_project.question.
- "cancel": a request to cancel, drop or stop a task or a project. cancel.task.id is the
  id the message writes (t-… or p-…). When it writes none, id is null and cancel.task.words
  is how it names the task, in its own words ("cancel the cafe task": "cafe"). Never make
  up an id.
- "set_tier": a request to run a task on the cheaper model ("economy") or the usual one
  ("premium"). set_tier.task as for cancel; set_tier.tier is "economy" or "premium".
- "chat": anything else. Answer in reply.
confidence is how sure you are of the intent, from 0 to 1. reply is at most two short
sentences. Fill the object named like the intent (status, new_project, cancel, set_tier),
never null for its own intent; leave the others null.
{language}
Answer with one JSON object only:
{{"intent": "chat" | "status" | "new_project" | "cancel" | "set_tier", "reply": "...",
 "confidence": 0.9,
 "status": {{"ids": ["t-1a2b3c4d"], "why": false}} or null,
 "new_project": {{"request": "...", "target": {{"kind": "new"}} or
 {{"kind": "existing", "path": "~/src/calc"}}, "question": "..." or null}} or null,
 "cancel": {{"task": {{"id": "t-1a2b3c4d" or null, "words": "..." or null}}}} or null,
 "set_tier": {{"task": {{"id": "t-1a2b3c4d" or null, "words": "..." or null}},
 "tier": "economy" | "premium"}} or null}}"""

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
    "existing_off": {
        "tr": "Var olan bir klasörde iş başlatmak burada açık değil; şimdilik: baton task add",
        "en": "Work in an existing directory is not set up here; for now: baton task add",
    },
    "relative": {
        "tr": "{path}: klasörün tam yolunu yaz (/… ya da ~/…).",
        "en": "{path}: write the full path (/… or ~/…).",
    },
    "missing": {
        "tr": "{path}: böyle bir klasör yok. baton klasör açmaz, taşımaz, silmez; yeni bir "
        "şey için yeni bir proje iste.",
        "en": "{path}: no such directory. baton makes, moves and deletes no directories; "
        "for something new, ask for a new project.",
    },
    "untrusted": {
        "tr": "{path}: policy.yaml'daki trusted_dirs içinde değil, baton'un açtığı bir proje "
        "de değil; orada iş başlatmam. Güveniyorsan trusted_dirs'e ekle.",
        "en": "{path}: not in the policy's trusted_dirs, and not a project baton made; I "
        "start no work there. Add it to trusted_dirs in policy.yaml if you trust it.",
    },
    "unknown_project": {
        "tr": "{path}: kayıtta böyle bir proje yok.",
        "en": "{path}: no such project in the log.",
    },
    "by_trusted": {
        "tr": "policy.yaml'daki trusted_dirs içinde",
        "en": "in the policy's trusted_dirs",
    },
    "by_project": {"tr": "baton'un projesi {project}", "en": "it is baton's project {project}"},
    "task_card": {
        "tr": "🛠 İş önerisi\nBaşlık: {title}\n{request}\n\n"
        "Nerede: {where} (var olan klasör; baton klasör açmaz, taşımaz, silmez)\n"
        "Neden izinli: {why}\n"
        "Ajan (şu an): {agent}\n"
        "Katman: {tier}\n"
        "Başlatınca ajan doğrudan bu klasörde çalışır; plan aşaması yok.\n"
        "Kart {minutes} dakika geçerli.",
        "en": "🛠 Task proposal\nTitle: {title}\n{request}\n\n"
        "Where: {where} (an existing directory; baton makes, moves and deletes none)\n"
        "Allowed because: {why}\n"
        "Agent (now): {agent}\n"
        "Tier: {tier}\n"
        "Once you start it, the agent works in this directory; there is no plan step.\n"
        "This card can be pressed for {minutes} minutes.",
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
    # A cancel or a change of tier, asked for in words.
    "cancel_card": {
        "tr": "🛑 {task} iptal edilsin mi?\n"
        "Bırakılır ve yeniden denenmez{pane}.\n"
        "Kart {minutes} dakika geçerli.",
        "en": "🛑 Cancel {task}?\n"
        "It is dropped and not tried again{pane}.\n"
        "This card can be pressed for {minutes} minutes.",
    },
    "cancel_pane": {"tr": "; ajanının paneli kapatılır", "en": "; its agent's pane is closed"},
    "cancel_project_card": {
        "tr": "🛑 {project} projesi iptal edilsin mi?\n"
        "Açık {count} işi bırakılır, ajanlarının panelleri kapatılır:\n{tasks}\n"
        "Kart {minutes} dakika geçerli.",
        "en": "🛑 Cancel project {project}?\n"
        "Its {count} open task(s) are dropped and their agents' panes closed:\n{tasks}\n"
        "This card can be pressed for {minutes} minutes.",
    },
    "tier_card": {
        "tr": "🔀 {task} {tier} çalışsın mı?{now}\n"
        "Bir sonraki denemesinden geçerli{running}.\n"
        "Kart {minutes} dakika geçerli.",
        "en": "🔀 Run {task} {tier}?{now}\n"
        "It applies from its next attempt{running}.\n"
        "This card can be pressed for {minutes} minutes.",
    },
    "tier_now": {"tr": " Şu an: {tier}.", "en": " Now: {tier}."},
    "tier_running": {
        "tr": "; süren deneme kendi katmanında biter",
        "en": "; the attempt under way keeps its tier",
    },
    "economy": {"tr": "economy (ucuz model)", "en": "economy (the cheaper model)"},
    "premium": {"tr": "premium (her zamanki model)", "en": "premium (the usual model)"},
    "cancel_buttons": {"tr": "İptal et|Vazgeç", "en": "Cancel it|Keep it"},
    "tier_buttons": {"tr": "Değiştir|Vazgeç", "en": "Change|Keep"},
    "which": {
        "tr": "Hangisi? Kimliğini yaz:\n{choices}",
        "en": "Which one? Write its id:\n{choices}",
    },
    "which_more": {"tr": "… ve {count} tane daha", "en": "… and {count} more"},
    "which_none": {
        "tr": "Hangi iş? Kimliğini yaz (t-…); /status hepsini gösterir.",
        "en": "Which task? Write its id (t-…); /status lists them.",
    },
    "no_match": {
        "tr": "'{words}' diye açık bir iş bulamadım. Kimliğini yaz; /status hepsini gösterir.",
        "en": "No open task fits '{words}'. Write its id; /status lists them.",
    },
    "closed": {
        "tr": "{named} zaten {status}; değişecek bir şey yok.",
        "en": "{named} is already {status}; nothing to change.",
    },
    "same_tier": {
        "tr": "{task} zaten {tier} çalışıyor.",
        "en": "{task} runs {tier} already.",
    },
    "no_such_button": {
        "tr": "Bu kartta böyle bir düğme yok.",
        "en": "This card has no such button.",
    },
    "already_done": {"tr": "Zaten yapıldı. {result}", "en": "Already done. {result}"},
    "kept": {
        "tr": "Olduğu gibi bırakıldı; hiçbir şey değişmedi.",
        "en": "Left as it was; nothing changed.",
    },
    "was_kept": {
        "tr": "Bu karttan vazgeçilmişti; hiçbir şey değişmedi.",
        "en": "This card was dismissed; nothing changed.",
    },
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
        "iptal",
        "et",
        "ucuz",
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
    """What a card says besides the request, from the bot: where a project would be made,
    the agent a task would go to now and why, and the tier its plan (or the task) runs in."""

    where: str
    agent: str
    tier: str


# The request, and the directory it would run in (None: a new project).
type CardContextFor = Callable[[str, str | None], Awaitable[CardContext]]
# A card's Start: the command the card stands for, run by the bot; whether it did it, and
# what it said.
type CardAction = Callable[[Card], Awaitable[tuple[bool, str]]]
# A task's title, instructions and directory, added as ``baton task add`` adds one.
type AddTask = Callable[[str, str, str], Awaitable[tuple[bool, str]]]


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
        places: Places | None = None,
    ) -> None:
        self._backend = backend
        self._store = store
        self._config = config
        # Proposed projects waiting for a press (A3); None: the bot shows no cards.
        self._cards = cards
        # Directories that may take work (trusted_dirs, baton's projects); None: work in an
        # existing directory is not offered.
        self._places = places
        self._busy = asyncio.Lock()
        self._history: deque[ChatMessage] = deque(maxlen=config.history_messages)
        # A cancel or a tier asked "which one?": the operator's next message may be the id.
        self._pending: tuple[CardKind, str | None] | None = None
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

    async def press(self, data: str, start: CardAction, add: AddTask | None = None) -> str:
        """A press on a card's button (``as:<action>:<card>``). ``start`` does what the
        card stands for the way its command does (/new, /cancel, /tier); ``add`` adds the
        task in a directory that exists the way ``baton task add`` does. At most one runs,
        once per card."""
        _, action, card_id = [*data.split(":", 2), "", ""][:3]
        if self._cards is None:
            return TEXTS["expired"]["en"]

        async def begin(card: Card) -> tuple[bool, str]:
            if card.workdir is None:
                return await start(card)
            language: Language = "tr" if card.language == "tr" else "en"
            if self._places is None or add is None:
                return False, TEXTS["existing_off"][language]
            # Checked again: the directory may have gone, or the policy changed, meanwhile.
            found = self._places.find(card.workdir, await self._store.read())
            if isinstance(found, Refused):
                return False, _refusal(found, language)
            return await add(card.title, card.request, found.path)

        pressed = await self._cards.press(card_id, action, begin)
        card = pressed.card
        if card is None:
            return TEXTS["expired"]["en"]
        language: Language = "tr" if card.language == "tr" else "en"
        self._log.info("assistant card", card=card.card_id, action=action, state=card.state)
        return _press_text(card, acted=pressed.acted, language=language)

    async def _answer(  # noqa: PLR0911 - one return per kind of answer
        self, message: str, language: Language, context: CardContextFor | None
    ) -> Reply:
        editing = self._cards.editing() if self._cards is not None else None
        pending, self._pending = self._pending, None
        if pending is not None and (answered := _only_an_id(message)) is not None:
            # The answer to "which one?": the id, written by the operator.
            kind, tier = pending
            return await self._action(kind, TaskRef(id=answered), tier, message, language)
        if (known := plain_status(message)) is not None:
            # The intent is plain without the model: a call saved, the slowest part.
            self._log.info("assistant intent", intent="status", why=known.why, model=False)
            interpretation = Interpretation(intent="status", reply="", confidence=1.0, status=known)
            return Reply(await self._act(interpretation, message, language))
        if editing is not None:
            # The operator pressed Edit: this message is the change to that request.
            where = EDIT_WHERE.format(path=editing.workdir) if editing.workdir else ""
            message = EDIT_FRAME.format(request=editing.request, where=where, change=message)
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
        interpretation = parsed.interpretation
        if interpretation.cancel is not None:
            return await self._action(
                CardKind.CANCEL, interpretation.cancel.task, None, message, language
            )
        if interpretation.set_tier is not None:
            wanted = interpretation.set_tier
            return await self._action(CardKind.TIER, wanted.task, wanted.tier, message, language)
        proposal = parsed.interpretation.new_project
        if proposal is not None and not proposal.question and self._cards is not None:
            place: Place | None = None
            if proposal.target.path is not None:
                if self._places is None:
                    return Reply(TEXTS["existing_off"][language])
                found = self._places.find(proposal.target.path, await self._store.read())
                self._log.info("assistant directory", found=found)
                if isinstance(found, Refused):
                    return Reply(_refusal(found, language))
                place = found
            if editing is not None:
                self._cards.supersede(editing.card_id)
            return await self._card(proposal.request, language, context, place)
        return Reply(await self._act(parsed.interpretation, message, language))

    async def _card(
        self,
        request: str,
        language: Language,
        context: CardContextFor | None,
        place: Place | None = None,
    ) -> Reply:
        assert self._cards is not None  # noqa: S101 - the caller checked
        workdir = place.path if place is not None else None
        title = title_of(request) if place is not None else ""
        card = self._cards.propose(request, language=language, workdir=workdir, title=title)
        about = (
            await context(request, workdir) if context is not None else CardContext("-", "-", "-")
        )
        minutes = int(self._cards.ttl.total_seconds() // 60)
        if place is None:
            text = TEXTS["card"][language].format(
                request=request,
                where=about.where,
                agent=about.agent,
                tier=about.tier,
                minutes=minutes,
            )
        else:
            why = (
                TEXTS["by_trusted"][language]
                if place.by == "trusted_dirs"
                else TEXTS["by_project"][language].format(project=place.by)
            )
            text = TEXTS["task_card"][language].format(
                title=title,
                request=request,
                where=place.path,
                why=why,
                agent=about.agent,
                tier=about.tier,
                minutes=minutes,
            )
        labels = TEXTS["buttons"][language].split("|")
        actions = ("start", "edit", "cancel")
        buttons = tuple(
            (label, f"{CARD_PREFIX}{action}:{card.card_id}")
            for label, action in zip(labels, actions, strict=True)
        )
        return Reply(text, buttons)

    async def _action(
        self, kind: CardKind, ref: TaskRef, tier: str | None, message: str, language: Language
    ) -> Reply:
        """A cancel (``tier`` None) or a change of tier: a card naming the task and what
        will happen, or a question when which task is not plain."""
        written = (message, *(m.text for m in self._history if m.role == "operator"))
        found = find_target(
            project(await self._store.read()),
            ref,
            written=written,
            projects=kind is CardKind.CANCEL,
        )
        self._log.info("assistant target", kind=kind, found=type(found).__name__)
        if isinstance(found, (Unknown, Ambiguous)):
            self._pending = (kind, tier)  # the next message may be the id
            return Reply(_which(found, language))
        if isinstance(found, Closed):
            return Reply(TEXTS["closed"][language].format(named=found.named, status=found.status))
        if isinstance(found, OneTask) and tier is not None and found.task.tier == tier:
            return Reply(
                TEXTS["same_tier"][language].format(
                    task=_task_line(found.task), tier=TEXTS[tier][language]
                )
            )
        target = found.task.task_id if isinstance(found, OneTask) else found.project_id
        request = f"{target} {tier}" if tier is not None else target
        if self._cards is None:
            return Reply(f"/{kind.value} {request}")  # no cards: the command that would do it
        card = self._cards.propose(request, language=language, kind=kind)
        minutes = int(self._cards.ttl.total_seconds() // 60)
        labels = TEXTS[f"{kind.value}_buttons"][language].split("|")
        buttons = tuple(
            (label, f"{CARD_PREFIX}{action}:{card.card_id}")
            for label, action in zip(labels, ("start", "cancel"), strict=True)
        )
        return Reply(_action_text(found, tier, language, minutes), buttons)

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
            if proposal.target.kind == "existing":
                return TEXTS["existing_off"][language]
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


def _only_an_id(message: str) -> str | None:
    """The id a message is made of, alone: "t-1a2b3c4d", "t-1a2b3c4d."."""
    ids = _IDS.findall(message)
    rest = _IDS.sub("", message).strip(" \t\n.,!?'\"`")
    return ids[0] if len(ids) == 1 and not rest else None


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
    if words & (_BUILD | _CHANGE) or len(ids) > MAX_IDS:
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
# Words that ask for a task to be cancelled or moved: such a message goes to the model too.
_CHANGE = frozenset(
    {"iptal", "durdur", "cancel", "stop", "drop", "ucuz", "economy", "premium", "tier"}
)
MAX_IDS = 5
MAX_OPEN_WORDS = 6


# An edit, put to the model as one message: the request as it stood, and what changes.
EDIT_FRAME = (
    "Revise this project request and propose it again as new_project.\n"
    "Request: {request}\n{where}Change: {change}"
)
# The directory of an edited card, so the revision keeps it unless the change names another.
EDIT_WHERE = "Directory: {path}\n(target existing, this path, unless the change names another)\n"

# A task's title from its request: the first sentence, short enough for /status.
TITLE_LENGTH = 60
_SENTENCE_END = re.compile(r"[.;!?](?:\s|$)|\n")
_TAG = re.compile(r"(?<!\S)#\w+")


def title_of(request: str) -> str:
    """The first sentence of ``request``, without tags (#economy), cut to a word under
    :data:`TITLE_LENGTH` characters."""
    text = _TAG.sub("", request).strip()
    first = _SENTENCE_END.split(text, maxsplit=1)[0].strip() or text
    if len(first) <= TITLE_LENGTH:
        return first
    cut = first[: TITLE_LENGTH - 1].rsplit(" ", 1)[0].rstrip(",;: ")
    return f"{cut}…"


def _refusal(refused: Refused, language: Language) -> str:
    return TEXTS[refused.reason][language].format(path=refused.path)


def _press_text(card: Card, *, acted: bool, language: Language) -> str:  # noqa: PLR0911 - one per state
    """What a press is answered with: what it did, or the state that kept it from acting."""
    a_project = card.kind is CardKind.PROJECT
    match card.state:
        case CardState.STARTED:
            already = "already_started" if a_project else "already_done"
            return card.result if acted else TEXTS[already][language].format(result=card.result)
        case CardState.FAILED:
            return card.result
        case CardState.EDITING:
            return TEXTS["edit"][language]
        case CardState.CANCELLED if a_project:
            return TEXTS["cancelled" if acted else "was_cancelled"][language]
        case CardState.CANCELLED:
            return TEXTS["kept" if acted else "was_kept"][language]
        case CardState.STARTING:
            return TEXTS["starting"][language]
        case CardState.SUPERSEDED:
            return TEXTS["superseded"][language]
        case CardState.PROPOSED:  # a button this card does not have: Edit on a cancel
            return TEXTS["no_such_button"][language]
        case _:
            return TEXTS["expired"][language]


def _brief(digest: Digest, language: Language) -> str:
    lines = brief(digest) or ([] if digest.unknown else [TEXTS["nothing_open"][language]])
    if digest.unknown:
        lines.append(TEXTS["unknown"][language].format(ids=", ".join(digest.unknown)))
    return "\n".join(lines)


def _task_line(task: TaskView) -> str:
    """A task as a card names it: its id, title, status and agent."""
    agent = f", {task.attempts[-1].agent.value}" if task.attempts else ""
    return f'{task.task_id} "{task.title}" ({task.status.value}{agent})'


def _action_text(
    found: OneTask | OneProject, tier: str | None, language: Language, minutes: int
) -> str:
    if isinstance(found, OneProject):
        return TEXTS["cancel_project_card"][language].format(
            project=found.project_id,
            count=len(found.open_tasks),
            tasks="\n".join(f"- {_task_line(t)}" for t in found.open_tasks),
            minutes=minutes,
        )
    task = found.task
    live = task.live_attempt
    if tier is None:
        pane = TEXTS["cancel_pane"][language] if live and live.pane_id else ""
        return TEXTS["cancel_card"][language].format(
            task=_task_line(task), pane=pane, minutes=minutes
        )
    now = task.tier or (task.attempts[-1].tier if task.attempts else None)
    return TEXTS["tier_card"][language].format(
        task=_task_line(task),
        tier=TEXTS[tier][language],
        now=TEXTS["tier_now"][language].format(tier=now.value) if now else "",
        running=TEXTS["tier_running"][language] if live else "",
        minutes=minutes,
    )


def _which(found: Found, language: Language) -> str:
    """The question asked when which task is not plain: the candidates, or what failed."""
    if isinstance(found, Ambiguous):
        lines = [f"- {_task_line(t)}" for t in found.candidates[:MAX_CHOICES]]
        if len(found.candidates) > MAX_CHOICES:
            more = len(found.candidates) - MAX_CHOICES
            lines.append(TEXTS["which_more"][language].format(count=more))
        return TEXTS["which"][language].format(choices="\n".join(lines))
    assert isinstance(found, Unknown)  # noqa: S101 - the caller passes only these two
    if not found.named:
        return TEXTS["which_none"][language]
    if _IDS.fullmatch(found.named):
        return TEXTS["unknown"][language].format(ids=found.named)
    return TEXTS["no_match"][language].format(words=found.named)
