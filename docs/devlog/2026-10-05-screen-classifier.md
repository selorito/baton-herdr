# 2026-10-05: baton-detect, phase 2: the screen classifier

`baton-detect classify` answers detection requests as `baton detect` does, one NDJSON line
in and one out:

- The rules moved into data: `crates/baton-detect/rules/{claude,codex,opencode}.toml`.
  The field names are herdr's; the patterns are the Python ones, verbatim.
- Python's line handling is copied: `splitlines`, `strip` and whitespace, including the
  separators Python counts as line breaks. So is how it resolves local times: a time in a
  daylight-saving gap gets the offset before the gap, and wall-clock comparisons are made in
  the same zone.
- Parity: 1,949 cases, from all recorded captures under every host state, limit messages
  around DST changes in seven zones, the fake agents' screens and edge cases. All equal on
  the first run. To check that the test can fail, the Codex idle rule was given one changed
  character; exactly the one case written for it failed.
- batond: `[detector] engine = "python" | "shadow" | "rust"`. `shadow` logs every difference
  as "detector mismatch"; `rust` falls back to Python while the process is down. A whole
  slice-0 task run in shadow mode gives 0 mismatches. `baton doctor` checks the binary with
  a test screen.

Next: `shadow` on real work, then `rust` as the default and the Python rules removed.
