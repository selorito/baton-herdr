"""Confirmation cards: what the assistant proposes, done only on the operator's press
(ADR 0021).

A card is proposed with what it would pass to its command: the request for ``/new``, or
the arguments of ``/cancel`` or ``/tier``. Its Start calls the very function that command
calls, once: presses are decided under a lock per card, and a card in a final state
answers with that state. Only a project's card can be edited. Cards live in memory; after
a restart, or once their time is up, a press is told the card has expired. Nothing was
started from it.

    PROPOSED --start--> STARTING --ok--> STARTED
       |                    '--error--> FAILED
       |--edit--> EDITING --the next message--> a new card; this one SUPERSEDED
       |--cancel--> CANCELLED
       '--time--> EXPIRED (from PROPOSED or EDITING)
"""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from datetime import datetime, timedelta

    from baton_herdr.core.ports import Clock

# Button data: "as:<action>:<card id>", well under Telegram's 64 bytes.
PREFIX = "as:"


class CardState(StrEnum):
    PROPOSED = "proposed"
    EDITING = "editing"
    STARTING = "starting"
    STARTED = "started"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"

    @property
    def final(self) -> bool:
        return self not in {CardState.PROPOSED, CardState.EDITING, CardState.STARTING}


class CardKind(StrEnum):
    PROJECT = "project"  # /new <request>
    CANCEL = "cancel"  # /cancel <t-… or p-…>
    TIER = "tier"  # /tier <t-…> <economy|premium>


@dataclass(frozen=True, slots=True)
class Card:
    card_id: str
    # What its command is given: a project's request, or a command's arguments.
    request: str
    created_at: datetime
    # The language its texts are in: the operator's when it was proposed.
    language: str = "en"
    state: CardState = CardState.PROPOSED
    # What starting it said: the project made, or why it could not be.
    result: str = ""
    kind: CardKind = CardKind.PROJECT


@dataclass(frozen=True, slots=True)
class Press:
    """What a press did: the card as it is now, and whether this press changed it."""

    card: Card | None
    acted: bool


@dataclass
class CardStore:
    clock: Clock
    ttl: timedelta
    _cards: dict[str, Card] = field(default_factory=dict)
    _locks: dict[str, asyncio.Lock] = field(default_factory=dict)

    def propose(
        self, request: str, *, language: str = "en", kind: CardKind = CardKind.PROJECT
    ) -> Card:
        card = Card(
            card_id=secrets.token_hex(4),
            request=request,
            created_at=self.clock.now(),
            language=language,
            kind=kind,
        )
        self._cards[card.card_id] = card
        self._locks[card.card_id] = asyncio.Lock()
        return card

    def get(self, card_id: str) -> Card | None:
        card = self._cards.get(card_id)
        if card is not None and not card.state.final and self._expired(card):
            card = self._set(card, CardState.EXPIRED)
        return card

    def editing(self) -> Card | None:
        """The card waiting for the operator's change, if one is."""
        for card_id in list(self._cards):
            card = self.get(card_id)
            if card is not None and card.state is CardState.EDITING:
                return card
        return None

    def supersede(self, card_id: str) -> None:
        card = self._cards.get(card_id)
        if card is not None and not card.state.final:
            self._set(card, CardState.SUPERSEDED)

    async def press(
        self, card_id: str, action: str, start: Callable[[Card], Awaitable[tuple[bool, str]]]
    ) -> Press:
        """Act on a press of ``action`` ("start", "edit", "cancel") once. ``start`` is
        given the card and says whether its command did it, and what to tell."""
        lock = self._locks.get(card_id)
        if lock is None:
            return Press(card=None, acted=False)  # unknown: made before a restart
        async with lock:
            card = self.get(card_id)
            if card is None or card.state is not CardState.PROPOSED:
                return Press(card=card, acted=False)
            if action == "cancel":
                return Press(card=self._set(card, CardState.CANCELLED), acted=True)
            if action == "edit" and card.kind is CardKind.PROJECT:
                for other in list(self._cards.values()):
                    if other.state is CardState.EDITING:
                        self._set(other, CardState.SUPERSEDED)  # one change at a time
                return Press(card=self._set(card, CardState.EDITING), acted=True)
            if action != "start":
                return Press(card=card, acted=False)
            self._set(card, CardState.STARTING)
            try:
                made, said = await start(card)
            except Exception:
                self._set(card, CardState.FAILED, "could not be started")
                raise
            state = CardState.STARTED if made else CardState.FAILED
            return Press(card=self._set(card, state, said), acted=True)

    def _expired(self, card: Card) -> bool:
        return self.clock.now() - card.created_at > self.ttl

    def _set(self, card: Card, state: CardState, result: str | None = None) -> Card:
        changed = replace(card, state=state, result=card.result if result is None else result)
        self._cards[card.card_id] = changed
        return changed
