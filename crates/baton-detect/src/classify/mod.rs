//! The screen classifier (ADR 0011, phase 2): one `DetectionRequest` in, one
//! `DetectionResult` out (`schemas/detector-*.v1.json`).
//!
//! It gives the same answers as the Python detector (`baton_herdr.core.detection` with
//! the adapters' rules); the parity tests hold the two to that. The rules are data,
//! `rules/<agent>.toml`, compiled into the binary.

pub mod clock;
pub mod rules;
pub mod text;

use jiff::Timestamp;
use jiff::tz::TimeZone;
use serde::{Deserialize, Serialize};

use rules::{Resets, RuleSet};

/// What an agent is doing, as baton understands it (ADR 0004).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AgentState {
    Working,
    Idle,
    BlockedPermission,
    BlockedQuestion,
    BlockedOther,
    RateLimited,
    ContextFull,
    Crashed,
    Unknown,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AgentKind {
    Claude,
    Codex,
    Gemini,
    Opencode,
}

/// The contract version both sides speak; a request with another one is refused.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(try_from = "u8", into = "u8")]
pub struct Contract;

impl TryFrom<u8> for Contract {
    type Error = String;

    fn try_from(value: u8) -> Result<Self, Self::Error> {
        if value == 1 {
            Ok(Contract)
        } else {
            Err(format!(
                "contract {value} is not supported; this is contract 1"
            ))
        }
    }
}

impl From<Contract> for u8 {
    fn from(_: Contract) -> Self {
        1
    }
}

fn contract() -> Contract {
    Contract
}

fn running() -> bool {
    true
}

fn utc() -> String {
    "UTC".to_owned()
}

fn unknown() -> AgentState {
    AgentState::Unknown
}

/// One screen to classify (`schemas/detector-request.v1.json`).
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Request {
    #[serde(default = "contract")]
    pub contract: Contract,
    pub agent: AgentKind,
    #[serde(default)]
    pub agent_version: Option<String>,
    pub screen: String,
    /// What herdr concluded, already in baton's vocabulary.
    #[serde(default = "unknown")]
    pub host_state: AgentState,
    #[serde(default)]
    pub host_evidence: Option<String>,
    /// False when the host sees no agent process in the pane.
    #[serde(default = "running")]
    pub agent_running: bool,
    pub observed_at: Timestamp,
    /// IANA zone for clock times the agent prints.
    #[serde(default = "utc")]
    pub timezone: String,
}

/// The classification (`schemas/detector-result.v1.json`).
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Outcome {
    pub contract: Contract,
    pub state: AgentState,
    pub evidence: String,
    /// When a usage limit is expected to lift, if the screen says so.
    pub resets_at: Option<Timestamp>,
}

#[derive(Debug, thiserror::Error)]
pub enum ClassifyError {
    #[error("unknown time zone {0:?}")]
    TimeZone(String),
    #[error("rules for {agent}: {message}")]
    Rules {
        agent: &'static str,
        message: String,
    },
}

fn outcome(state: AgentState, evidence: impl Into<String>) -> Outcome {
    Outcome {
        contract: Contract,
        state,
        evidence: evidence.into(),
        resets_at: None,
    }
}

/// Classify one screen: the first matching rule wins, else the host's conclusion stands.
///
/// # Errors
/// The request names a time zone that does not exist, or the compiled-in rules are broken.
pub fn classify(request: &Request) -> Result<Outcome, ClassifyError> {
    let Some(rules) = rules::for_agent(request.agent)? else {
        return Ok(outcome(request.host_state, "baton:no-adapter"));
    };
    let result = detect(request, rules)?;
    Ok(rules.refine_blocked(result))
}

fn detect(request: &Request, rules: &RuleSet) -> Result<Outcome, ClassifyError> {
    if !request.agent_running {
        let evidence = request.host_evidence.as_deref().unwrap_or("host:no-agent");
        return Ok(outcome(request.host_state, evidence));
    }
    let zone = time_zone(&request.timezone)
        .ok_or_else(|| ClassifyError::TimeZone(request.timezone.clone()))?;
    for rule in &rules.rules {
        if rule
            .applies_when
            .as_ref()
            .is_some_and(|states| !states.contains(&request.host_state))
        {
            continue;
        }
        let region = text::bottom(&request.screen, rule.bottom_lines);
        // A pattern that gives up (backtracking limit) is no match, as no answer is better
        // than a wrong one; the parity tests would show it.
        let Ok(Some(caps)) = rule.regex.captures(region.as_str()) else {
            continue;
        };
        let resets_at = match rule.resets {
            Resets::Never => None,
            Resets::FromGroups => resets_from(&caps, request.observed_at, &zone),
        };
        return Ok(Outcome {
            resets_at,
            ..outcome(rule.state, format!("baton:{}", rule.id))
        });
    }
    let evidence = request
        .host_evidence
        .as_deref()
        .unwrap_or("host:no-evidence");
    Ok(outcome(request.host_state, evidence))
}

/// An IANA zone by its exact name, as Python's `zoneinfo` finds it on Linux (jiff alone
/// would also accept other spellings of the name).
#[must_use]
pub fn time_zone(name: &str) -> Option<TimeZone> {
    if name == "UTC" {
        return Some(TimeZone::UTC);
    }
    let zone = TimeZone::get(name).ok()?;
    (zone.iana_name() == Some(name)).then_some(zone)
}

/// The reset time a limit message names, from the rule's named groups: `clock` (with
/// `weekday` and `zone`), `at` (a full local date) or `in` (a duration).
fn resets_from(
    caps: &fancy_regex::Captures<'_, str>,
    observed_at: Timestamp,
    zone: &TimeZone,
) -> Option<Timestamp> {
    if let Some(clock) = caps.name("clock") {
        // A zone printed after the time wins, if it exists.
        let printed = caps.name("zone").and_then(|m| time_zone(m.as_str()));
        let weekday = caps.name("weekday").map(|m| m.as_str());
        return clock::next_clock_time(
            clock.as_str(),
            observed_at,
            printed.as_ref().unwrap_or(zone),
            weekday,
        );
    }
    if let Some(at) = caps.name("at") {
        let words: Vec<&str> = at
            .as_str()
            .split(text::is_space)
            .filter(|w| !w.is_empty())
            .collect();
        return clock::parse_month_date_time(&words.join(" "), zone);
    }
    if let Some(duration) = caps.name("in") {
        let seconds = clock::parse_duration(duration.as_str())?;
        return observed_at
            .checked_add(jiff::SignedDuration::from_secs(seconds))
            .ok();
    }
    None
}
