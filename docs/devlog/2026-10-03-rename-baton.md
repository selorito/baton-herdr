# 2026-10-03: renamed to baton-herdr

The project is now **baton-herdr**: import package `baton_herdr`, command `baton`, daemon
`batond`, settings in `~/.config/baton`, environment prefix `BATON_`, end-of-turn marker
`[[BATON:END …]]`. The full list of name layers is in ADR 0010.

- Everything outside `fixtures/`, `docs/adr/` and `docs/devlog/` was renamed; those keep the
  old name as a record.
- The fixtures audit now allows any `~/dev/<name>-sandbox`, so captures made in the old
  sandbox stay valid without being rewritten.
- The README has a section for moving an existing install (services, CLI, settings, event
  log).
