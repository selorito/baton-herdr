# 2026-11-10: lost events under load (ROADMAP step 6)

Branch `test/events-lost`. Question: does baton end every attempt in the state its screen
shows when herdr's subscription loses events, with nothing in the log twice or missing?

## How the watch path works

`HerdrPaneHost.watch` subscribes, reads the pane, and reads again on every event; after
`events_lost` it subscribes again and reads, so a change made in the gap still arrives by
the read. The runner consumes it through `_ObservationFeed`, which also reads the pane
every `poll_interval_s` when no event comes. In the tests below that read is set to 600 s,
beyond the turn timeout, so only the watch can tell the runner anything.

## What was tried

Scheduler, with fakes (`tests/integration/test_events_lost.py`). `LossyAgents` is the
simulated Claude (limit script) and Codex (finish script) with a watch that loses changes
at random: lost, then a new subscription and a read after a gap during which the agent
goes on; bursts of the same observation; the watch given up with
`PaneHostUnavailableError`. 8 to 16 tasks run side by side, so most Claude attempts hit the
limit and are handed to Codex, the rest start on Codex.

| Case | Result |
|------|--------|
| 16 tasks, 40 % of changes lost, 30 % in bursts | pass |
| Every turn's last change (limit screen, finished turn) lost, 8 tasks | pass |
| Random losses and bursts, 5 seeds, panes closed or kept | pass |
| Watch given up on every turn's last change, through batond's loop | pass, after a failed run each |
| Watch given up at random (25 %), 3 seeds, through batond's loop | pass, after failed runs |

Each asserts: every task completed with one completion notice; per attempt one start,
one prompt (no nudge, no second send), one end, at most one interruption; no state
recorded twice in a row; Claude attempts end `rate_limited` and abandoned, Codex attempts
`idle` and succeeded.

herdr host, against a stand-in herdr with many panes (`test_herdr_watch_lost.py`): 24
panes watched at once, 400 changes, each sent as an event, a burst of five, `events_lost`,
or the end of the connection; every watch has to end on its pane's current state.

## What broke

The runner itself: nothing. Lost events and bursts are reconciled by the read after the
new subscription, and the runner logs a state only when it changes.

The herdr host: a subscription whose connection ended (herdr closed it without a message)
raised `PaneHostUnavailableError` instead of subscribing again. The runner passed that on,
the task's run failed mid-turn, and only batond's next cycle re-attached to the attempt.
The re-attach itself is clean (the scheduler tests above show it), but it cost a traceback
in the log and a wait for the next cycle. Fixed in `fix(herdr): open a subscription again
when its connection ends`: an ended subscription is handled like `events_lost`; only a
subscription herdr does not take again is reported as unavailable.

Not done: a live test that makes herdr report `events_lost`. That needs herdr's event
buffer to overflow for one subscriber, which cannot be caused on purpose from outside.
herdr's own behaviour after `events_lost` (sending the error and closing, or keeping the
connection) does not matter now: both paths subscribe again. A herdr that accepted
subscriptions and closed them at once, forever, would make the watch read the pane in a
loop; nothing like that has been seen.

## Checks

`just check-py` on WSL1: everything passes except the two `test_classify_golden.py` tests,
which fail before this branch too: the installed `baton-detect` here answers `unknown time
zone "US/Eastern"` (no Rust toolchain on this machine to rebuild it). `just smoke`: 9
passed, 1 skipped (no Chromium).
