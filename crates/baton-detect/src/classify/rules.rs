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

use serde::Deserialize;

use super::{AgentKind, AgentState, ClassifyError, Outcome};

const CLAUDE: &str = include_str!("../../rules/claude.toml");
const CODEX: &str = include_str!("../../rules/codex.toml");
const OPENCODE: &str = include_str!("../../rules/opencode.toml");

const REGION: &str = "bottom_non_empty_trimmed";
/// How many times a look-around pattern may backtrack on one screen before it gives up.
/// Every rule today stays below a thousand on the recorded screens; a screen built to
/// make a pattern backtrack hits this in milliseconds instead of hanging the classifier.
pub const BACKTRACK_LIMIT: usize = 100_000;
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
    pub pattern: Pattern,
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
                let pattern = Pattern::new(&rule.regex)
                    .map_err(|e| format!("rule {}: pattern: {e}", rule.id))?;
                Ok(Rule {
                    id: rule.id,
                    state: rule.state,
                    bottom_lines,
                    pattern,
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

/// A rule's pattern, on the engine it needs.
///
/// Most patterns run on `regex`, whose matching time is linear in the screen. Only a
/// pattern that needs look-around (which `regex` does not have) runs on `fancy-regex`,
/// a backtracking engine, and then with [`BACKTRACK_LIMIT`]. Both read the same syntax,
/// so a pattern means the same on either.
#[derive(Debug)]
pub enum Pattern {
    Linear(regex::Regex),
    Backtracking(fancy_regex::Regex),
}

/// The outcome of one search.
#[derive(Debug, PartialEq, Eq)]
pub enum Found<'t> {
    /// A match, with the named groups that took part in it.
    Match(Vec<(String, &'t str)>),
    NoMatch,
    /// The backtracking limit was reached: no answer, rather than a slow or wrong one.
    GaveUp,
}

impl<'t> Found<'t> {
    #[must_use]
    pub fn group(&self, name: &str) -> Option<&'t str> {
        match self {
            Found::Match(groups) => groups.iter().find(|(n, _)| n == name).map(|(_, t)| *t),
            Found::NoMatch | Found::GaveUp => None,
        }
    }
}

impl Pattern {
    /// Compile `source` on the linear engine if it can, else on the backtracking one.
    ///
    /// # Errors
    /// Neither engine compiles the pattern.
    pub fn new(source: &str) -> Result<Self, String> {
        if let Ok(linear) = regex::Regex::new(source) {
            return Ok(Self::Linear(linear));
        }
        fancy_regex::RegexBuilder::new(source)
            .backtrack_limit(BACKTRACK_LIMIT)
            .build()
            .map(Self::Backtracking)
            .map_err(|e| e.to_string())
    }

    #[must_use]
    pub fn is_backtracking(&self) -> bool {
        matches!(self, Self::Backtracking(_))
    }

    #[must_use]
    pub fn find<'t>(&self, text: &'t str) -> Found<'t> {
        match self {
            Self::Linear(regex) => match regex.captures(text) {
                Some(caps) => Found::Match(named(regex.capture_names(), |n| caps.name(n))),
                None => Found::NoMatch,
            },
            Self::Backtracking(regex) => match regex.captures(text) {
                Ok(Some(caps)) => Found::Match(named(regex.capture_names(), |n| caps.name(n))),
                Ok(None) => Found::NoMatch,
                Err(_) => Found::GaveUp,
            },
        }
    }
}

fn named<'n, 't, M: Matched<'t>>(
    names: impl Iterator<Item = Option<&'n str>>,
    group: impl Fn(&str) -> Option<M>,
) -> Vec<(String, &'t str)> {
    names
        .flatten()
        .filter_map(|name| group(name).map(|m| (name.to_owned(), m.text())))
        .collect()
}

/// A matched group on either engine.
trait Matched<'t> {
    fn text(&self) -> &'t str;
}

impl<'t> Matched<'t> for regex::Match<'t> {
    fn text(&self) -> &'t str {
        self.as_str()
    }
}

impl<'t> Matched<'t> for fancy_regex::Match<'t> {
    fn text(&self) -> &'t str {
        self.as_str()
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

/// Rules read from a directory at run time (`classify --rules DIR`): `DIR/<agent>.toml`,
/// each in the format above. For trying out a change to the rules without a rebuild, and
/// for the mutation tests; batond uses the compiled-in rules.
#[derive(Debug, Default)]
pub struct RuleBook {
    sets: Vec<RuleSet>,
}

impl RuleBook {
    /// Read every `<agent>.toml` in `dir`; an agent without a file has no rules.
    ///
    /// # Errors
    /// The directory cannot be read, or a file is not a valid rules file.
    pub fn load(dir: &std::path::Path) -> Result<Self, String> {
        let mut sets = Vec::new();
        for name in ["claude", "codex", "opencode"] {
            let path = dir.join(format!("{name}.toml"));
            let source = match std::fs::read_to_string(&path) {
                Ok(source) => source,
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
                Err(error) => return Err(format!("{}: {error}", path.display())),
            };
            sets.push(RuleSet::parse(&source).map_err(|e| format!("{}: {e}", path.display()))?);
        }
        Ok(Self { sets })
    }

    #[must_use]
    pub fn for_agent(&self, agent: AgentKind) -> Option<&RuleSet> {
        self.sets.iter().find(|set| set.agent == agent)
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

    #[test]
    fn only_rules_that_need_look_around_backtrack() {
        let backtracking: Vec<&str> = [AgentKind::Claude, AgentKind::Codex, AgentKind::Opencode]
            .into_iter()
            .flat_map(|agent| &for_agent(agent).unwrap().unwrap().rules)
            .filter(|rule| rule.pattern.is_backtracking())
            .map(|rule| rule.id.as_str())
            .collect();
        assert_eq!(
            backtracking,
            ["codex_idle_prompt", "opencode_idle_composer"]
        );
    }

    #[test]
    fn both_engines_report_named_groups() {
        for source in [r"in (?P<in>\d+) min", r"in (?P<in>\d+) min(?!utes)"] {
            let found = Pattern::new(source).unwrap().find("resets in 15 min.");
            assert_eq!(found.group("in"), Some("15"), "{source}");
            assert_eq!(found.group("clock"), None);
        }
        assert_eq!(Pattern::new("a(?=b)").unwrap().find("ac"), Found::NoMatch);
    }

    #[test]
    fn a_pattern_that_would_backtrack_for_ever_gives_up() {
        // Two ways to read each "a", so 2^60 ways to fail on a screen of sixty.
        let exponential = Pattern::new(r"^(?:a(?=a|c)|a)*c$").unwrap();
        let started = std::time::Instant::now();
        assert_eq!(exponential.find(&"a".repeat(60)), Found::GaveUp);
        assert!(started.elapsed() < std::time::Duration::from_secs(5));
        assert!(matches!(exponential.find("aaac"), Found::Match(_)));
    }
}
