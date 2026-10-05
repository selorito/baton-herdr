//! Screen rules as data: `rules/<agent>.toml`, compiled into the binary.
//!
//! The field names follow herdr's agent-detection manifests (`id`, `state`, `region`,
//! `regex`), so a rule can be read next to herdr's and offered upstream. A rule is one
//! pattern over one region; the first rule that matches wins.
//!
//! ```toml
//! agent = "claude"
//!
//! [refine_blocked]            # herdr rule id → the kind of blocked it means
//! bash_permission_prompt = "blocked_permission"
//!
//! [[rules]]
//! id = "claude_usage_limit"   # evidence: "baton:claude_usage_limit"
//! state = "rate_limited"
//! region = "bottom_non_empty_trimmed(30)"
//! regex = '''You've\s+hit …'''
//! resets = "from_groups"      # read a reset time from the named groups
//! applies_when = ["unknown"]  # only when herdr has nothing better (optional)
//! ```

use std::collections::HashMap;
use std::sync::LazyLock;

use fancy_regex::Regex;
use serde::Deserialize;

use super::{AgentKind, AgentState, ClassifyError, Outcome};

const CLAUDE: &str = include_str!("../../rules/claude.toml");
const CODEX: &str = include_str!("../../rules/codex.toml");
const OPENCODE: &str = include_str!("../../rules/opencode.toml");

const REGION: &str = "bottom_non_empty_trimmed";
const HOST_RULE: &str = "herdr:rule:";

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Resets {
    Never,
    FromGroups,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct FileSpec {
    agent: AgentKind,
    #[serde(default)]
    refine_blocked: HashMap<String, AgentState>,
    rules: Vec<RuleSpec>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct RuleSpec {
    id: String,
    state: AgentState,
    region: String,
    regex: String,
    #[serde(default)]
    applies_when: Option<Vec<AgentState>>,
    #[serde(default = "never")]
    resets: Resets,
}

fn never() -> Resets {
    Resets::Never
}

#[derive(Debug)]
pub struct Rule {
    pub id: String,
    pub state: AgentState,
    pub bottom_lines: usize,
    pub regex: Regex,
    pub applies_when: Option<Vec<AgentState>>,
    pub resets: Resets,
}

#[derive(Debug)]
pub struct RuleSet {
    pub agent: AgentKind,
    pub rules: Vec<Rule>,
    refine_blocked: HashMap<String, AgentState>,
}

impl RuleSet {
    /// Parse and compile one rules file.
    ///
    /// # Errors
    /// The file is not valid, a region is unknown or a pattern does not compile.
    pub fn parse(source: &str) -> Result<Self, String> {
        let spec: FileSpec = toml::from_str(source).map_err(|e| e.to_string())?;
        let rules = spec
            .rules
            .into_iter()
            .map(|rule| {
                let bottom_lines = region_lines(&rule.region)
                    .ok_or_else(|| format!("rule {}: unknown region {:?}", rule.id, rule.region))?;
                let regex = Regex::new(&rule.regex)
                    .map_err(|e| format!("rule {}: pattern: {e}", rule.id))?;
                Ok(Rule {
                    id: rule.id,
                    state: rule.state,
                    bottom_lines,
                    regex,
                    applies_when: rule.applies_when,
                    resets: rule.resets,
                })
            })
            .collect::<Result<_, String>>()?;
        Ok(Self {
            agent: spec.agent,
            rules,
            refine_blocked: spec.refine_blocked,
        })
    }

    /// Turn the host's `blocked_other` into a specific kind, by the herdr rule behind it.
    #[must_use]
    pub fn refine_blocked(&self, result: Outcome) -> Outcome {
        if result.state != AgentState::BlockedOther {
            return result;
        }
        let kind = result
            .evidence
            .strip_prefix(HOST_RULE)
            .and_then(|id| self.refine_blocked.get(id));
        match kind {
            Some(&state) => Outcome { state, ..result },
            None => result,
        }
    }
}

/// `bottom_non_empty_trimmed(N)`: the last N non-blank lines, right-trimmed. Unlike
/// herdr's `bottom_non_empty_lines`, blank lines in between are dropped too.
fn region_lines(region: &str) -> Option<usize> {
    region
        .trim()
        .strip_prefix(REGION)?
        .strip_prefix('(')?
        .strip_suffix(')')?
        .parse()
        .ok()
}

type Compiled = Result<RuleSet, String>;

static RULES: LazyLock<[(AgentKind, Compiled); 3]> = LazyLock::new(|| {
    [
        (AgentKind::Claude, RuleSet::parse(CLAUDE)),
        (AgentKind::Codex, RuleSet::parse(CODEX)),
        (AgentKind::Opencode, RuleSet::parse(OPENCODE)),
    ]
});

/// The compiled rules for `agent`, or `None` for an agent baton has no rules for.
///
/// # Errors
/// The compiled-in rules for `agent` are broken (a test catches this before release).
pub fn for_agent(agent: AgentKind) -> Result<Option<&'static RuleSet>, ClassifyError> {
    let Some((_, compiled)) = RULES.iter().find(|(kind, _)| *kind == agent) else {
        return Ok(None);
    };
    match compiled {
        Ok(rules) if rules.agent == agent => Ok(Some(rules)),
        Ok(_) => Err(error(agent, "the file names another agent".to_owned())),
        Err(message) => Err(error(agent, message.clone())),
    }
}

fn error(agent: AgentKind, message: String) -> ClassifyError {
    let agent = match agent {
        AgentKind::Claude => "claude",
        AgentKind::Codex => "codex",
        AgentKind::Gemini => "gemini",
        AgentKind::Opencode => "opencode",
    };
    ClassifyError::Rules { agent, message }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_compiled_in_rules_file_is_valid() {
        for agent in [AgentKind::Claude, AgentKind::Codex, AgentKind::Opencode] {
            let rules = for_agent(agent);
            assert!(matches!(rules, Ok(Some(_))), "{agent:?}: {:?}", rules.err());
        }
        assert!(matches!(for_agent(AgentKind::Gemini), Ok(None)));
    }

    #[test]
    fn regions_and_fields_are_checked() {
        let bad_region = "agent = \"codex\"\n[[rules]]\nid = \"x\"\nstate = \"idle\"\n\
                          region = \"whole_recent\"\nregex = \"a\"\n";
        assert_eq!(
            RuleSet::parse(bad_region).err().as_deref(),
            Some("rule x: unknown region \"whole_recent\"")
        );
        let unknown_field = "agent = \"codex\"\nrules = []\npriority = 1\n";
        assert!(RuleSet::parse(unknown_field).is_err());
        assert_eq!(region_lines(" bottom_non_empty_trimmed(12) "), Some(12));
    }
}
