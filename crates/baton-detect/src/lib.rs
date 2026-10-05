//! `baton-detect`: reads what coding agents leave behind and reports it to batond as
//! NDJSON (ADR 0011). Phase 1 is the usage collector, [`usage`]; phase 2 the screen
//! classifier, [`classify`].

pub mod classify;
pub mod tail;
pub mod usage;
