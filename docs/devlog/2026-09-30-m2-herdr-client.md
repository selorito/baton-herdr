# 2026-09-30: M2 herdr client

- `core/panes.py`: `PaneHost` protocol in coban's own vocabulary (`observe`, `watch`,
  `read_screen`, `send_prompt`, `send_text`, `send_keys`, `open_pane`, `close_pane`,
  `processes`), `PaneObservation`, and errors. `core/fakes.py`: `FakePaneHost`.
- `herdr/transport.py`: NDJSON over the Unix socket. One connection per request;
  `subscribe` returns only after herdr acknowledged, so callers can subscribe first and read
  second.
- `herdr/state.py`: `derive_state`, the ADR 0004 mapping. A rule-less `idle` becomes
  `unknown`; `done` is resolved through the screen state.
- `herdr/host.py`: `HerdrPaneHost`. `watch` treats events as invalidation signals and
  re-reads; after `events_lost` it resubscribes and reconciles with a fresh read.
- Tests: `derive_state` against every capture in `fixtures/` (no dialog or picker is ever
  `idle`); the client against a stand-in server on a temporary Unix socket; an opt-in live
  smoke test (`COBAN_LIVE_HERDR=1`) that passed against herdr 0.9.1.

The raw socket always wraps results in `{id, result}`; the bare objects seen in H0 were the
CLI's presentation of `agent explain` and `api schema`.

Next: adapters (Claude, Codex, OpenCode), one module each: launch and resume commands,
refining `blocked` into permission / question, positive idle detection where herdr has no
rule (Codex), and usage-log readers tested against `fixtures/<agent>/usage-sample.jsonl`.
