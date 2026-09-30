# Architecture Decision Records

Format: [MADR](https://adr.github.io/madr/). One file per decision, numbered, never renumbered.
To change a decision, add a new ADR that supersedes the old one and update the old one's status.

| ADR | Title | Status |
|-----|-------|--------|
| [0001](0001-hybrid-rust-python.md) | Hybrid Rust + Python architecture | accepted |
| [0002](0002-event-log-as-source-of-truth.md) | Append-only event log as the single source of truth | accepted |
| [0003](0003-no-herdr-fork-plugin-and-socket-only.md) | No herdr fork: integrate through the socket API and plugins only | accepted |
| [0004](0004-agent-state-model.md) | coban's agent state model on top of herdr's status | accepted |
| [0005](0005-automation-boundaries.md) | Boundaries for automating vendor agents | accepted |
