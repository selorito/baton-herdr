# 2026-10-03: baton-detect, phase 1: the usage collector

ADR 0011 moves `baton-detect` to Rust for reasons other than speed. Phase 1 is
`baton-detect usage`:

- Claude Code transcripts, Codex rollouts and OpenCode's database are read into one token
  vocabulary (`schemas/usage-event.v1.json`, generated from `baton_herdr.core.usage`).
- Claude: one record per `message.id`, across transcripts, since a resumed session can
  repeat earlier messages. Codex: cumulative counts become deltas; a resume continues the
  same file; a counter restart is recognised; the record id holds time and total.
  OpenCode: completed assistant messages, checked against the session totals.
- batond runs it, restarts it with backoff, and stores its lines in `usage_records` and
  `rate_limit_observations`, apart from the task event log; the database drops repeats.
- The capture tool now pseudonymises record ids (HMAC) instead of redacting them, so new
  samples can show deduplication; the existing samples cannot.

On this machine's logs: every session in 0.7 s, no duplicate ids, OpenCode's records equal
its session totals. Two Claude lines that no JSON parser accepts are reported and skipped.

Next: phase 2, the screen classifier in Rust with parity tests against the Python one; and
roadmap step 2's budget, from these records.
