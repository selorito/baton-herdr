"""What counts as progress for a working agent (ADR 0014)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from baton_herdr.scheduler.stall import ProgressWatch, fingerprint

T0 = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)
LIMIT = timedelta(minutes=15)


def test_what_moves_by_itself_is_not_a_change() -> None:
    a = "✻ Pondering… (3m 12s · esc to interrupt)\n  ⏸ manual mode on · 12.4k tokens"
    b = "✶ Pondering… (14m 59s · esc to interrupt)\n  ⏸ manual mode on · 98.1k tokens"
    assert fingerprint(a) == fingerprint(b)
    codex = "• Working (2s • esc to interrupt)\n  gpt · ~/dev · ⠏"
    assert fingerprint(codex) == fingerprint(codex.replace("2s", "9m 3s").replace("⠏", "⠹"))
    # New output is a change.
    assert fingerprint(a) != fingerprint(a + "\n● Read calc.py")


def test_quiet_only_when_neither_screen_nor_tokens_moved() -> None:
    watch = ProgressWatch()
    watch.working("● Step 1", T0)
    watch.working("● Step 1", T0 + timedelta(minutes=10))  # the same screen
    assert not watch.quiet(T0 + timedelta(minutes=14), LIMIT)
    assert watch.quiet(T0 + timedelta(minutes=15), LIMIT)
    watch.tokens(T0 + timedelta(minutes=12))  # a response was recorded
    assert not watch.quiet(T0 + timedelta(minutes=20), LIMIT)
    watch.tokens(T0 + timedelta(minutes=1))  # an older record changes nothing
    assert watch.quiet_for(T0 + timedelta(minutes=20)) == timedelta(minutes=8)
    watch.working("● Step 2", T0 + timedelta(minutes=30))
    assert watch.quiet_for(T0 + timedelta(minutes=31)) == timedelta(minutes=1)


def test_waiting_for_a_person_is_not_quiet_work() -> None:
    watch = ProgressWatch()
    watch.working("● Step 1", T0)
    watch.pause()
    assert not watch.quiet(T0 + timedelta(hours=2), LIMIT)
    watch.working("● Step 1", T0 + timedelta(hours=2))  # back to work: the clock restarts
    assert watch.quiet_for(T0 + timedelta(hours=2, minutes=1)) == timedelta(minutes=1)


def test_numbers_that_are_not_clocks_or_counters_still_count() -> None:
    assert fingerprint("● Step 1 of 9") != fingerprint("● Step 2 of 9")
    assert fingerprint("Ran 4 tests") != fingerprint("Ran 5 tests")
