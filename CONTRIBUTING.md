# Contributing

Thanks for looking. baton is small and opinionated; a short conversation first saves work
on both sides.

- **Bugs:** open an issue with the bug template. Include:
  - `baton version`, `baton doctor`, and the agent and herdr versions;
  - the lines of `journalctl --user -u baton` around the problem.

  Never paste tokens, keys or private paths.
- **A wrong state for an agent screen** (a limit not seen, a prompt misread): attach the
  screen. `tools/capture/` records one with paths, names and keys masked (see
  `fixtures/README.md`). Rules change only with a recorded screen as evidence.
- **Features:** open an issue first. Larger changes need an ADR in `docs/adr/` (see the
  README there); scope is set by ADR 0008.

## Making a change

```bash
uv sync
just install-hooks   # once: git push then runs just check first, and stops if it fails
just check           # lint, format, types, import contracts, Python and Rust tests, fixture audit
```

- `just check` must pass; CI runs the same, plus the Rust/Python parity job.
- Keep commits small, in [Conventional Commits](https://www.conventionalcommits.org/)
  style (`feat(scheduler): …`, `fix(detect): …`).
- New behaviour comes with tests. Decisions are pure functions tested as tables;
  `core/` imports nothing else from baton.
- Detection rules change in both `src/baton_herdr/adapters/` and
  `crates/baton-detect/rules/`. The parity test keeps them equal.
- User-visible changes get a line under `[Unreleased]` in `CHANGELOG.md`.

Contributions are licensed under Apache-2.0, as the project is.
