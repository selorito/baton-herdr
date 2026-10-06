---
status: accepted
date: 2026-10-05
revised: 2026-10-05 (tests and builds only in trusted directories); 2026-10-06 (Codex commands)
---

# A small permission policy: allow, ask or deny an agent's shell command, by rule

## Context and Problem Statement

Agents run in their default permission mode, so each shell command they want to run stops
the task on a prompt. Until now every prompt went to a person (ADR 0009). Most of them are
harmless: listing files, reading one, running the tests. An unattended run stalls on
exactly those, while the operator approves them by reflex. Approving by reflex is the
habit that lets a dangerous one through.

The alternative the agents offer, a mode that approves everything, gives up the one check
that stands between an agent and `git push --force` or `rm -rf`. What should baton approve
on its own, and how does a person see and change that?

## Decision Drivers

- Safe by default: what baton allows with no configuration must be harmless.
- Fail closed: a command baton cannot read with certainty goes to a person.
- Every decision explainable afterwards from the event log (ADR 0002), with the rule
  behind it.
- One path for answers: a policy answer must pass the same checks as an operator's
  (ADR 0009): the prompt still open, answered once, the pane verified.
- Small enough to test as a table, and to read in one sitting.

## Considered Options

1. Keep sending every prompt to a person.
2. Run the agents in their own auto-approve modes, with their own allow lists.
3. A baton policy: rules over the command on screen, decided in batond.

## Decision Outcome

Chosen option: **3**.

**Rules.** `~/.config/baton/policy.yaml` (`[policy] path`) holds the user's rules. They are
tried before built-in defaults (`policy/defaults.py`). A rule has:
- a decision: `allow`, `ask` or `deny`;
- either a shell-style `command` pattern (`git push*`) or a `regex`;
- optional `agents` it applies to;
- an optional `reason`;
- optional `trusted_only: true`: the allow holds only in `trusted_dirs`, elsewhere the
  command asks.

The file also lists `trusted_dirs`: the repositories whose own code (tests, builds) agents
may run without asking. A task's directory counts when it is one of them or inside one.

The first matching rule decides; no match means `ask`. A missing file means the defaults
alone. A file that does not parse stops batond from starting. `baton doctor` reports it,
and `baton policy check "<command>"` shows what would happen and why.

```yaml
trusted_dirs:
  - ~/src/calc
rules:
  - decision: allow
    command: "make lint*"
    reason: our linter
  - decision: deny
    regex: '^terraform\s+(apply|destroy)'
    agents: [codex]
```

**Defaults.** Only commands that read are allowed everywhere: `ls`, `cat`, `grep`, `rg`,
`find`, `git status`, `git diff`, `git log`, …

Tests and builds are allowed **only in `trusted_dirs`**, and ask everywhere else:
`pytest`, `python -m unittest`, `cargo test`/`build`, `go test`, `npm test`, `make`,
`just check`, ….

*Revision.* The first version allowed tests everywhere. That was wrong. A test command
runs whatever the repository's tests, `conftest.py`, `Makefile` or `package.json` scripts
contain, with the user's rights. That is the same reach as running an unknown script.
"Running the tests" is only as safe as the code in the directory. Whether that code is
trusted is the user's call, not baton's, so the user names the directories once. With no
`trusted_dirs`, every test run asks.

Asking rules come first, so no allow can let them through. They cover:
- deleting (`rm`);
- publishing and forcing (`git push`, `--force`);
- throwing work away (`git reset --hard`, `git clean`);
- migrations;
- `sudo`;
- the network (`curl`, `wget`, package installs, `git clone`/`fetch`/`pull`);
- `find -delete` and `find -exec`;
- options that turn a reading command into one that writes or runs something
  (`rg --pre`, `tree -o`, `git … --output`, `git … --ext-diff`).

There is no default `deny`: refusing is the user's choice to make.

**Reading a command.** Commands are split where the shell would run separate ones (`;`,
`&&`, `||`, `|`, `&`, newlines, parentheses). Each part is decided on its own, and the
strictest decision wins, so `pytest && git push` asks. Leading `NAME=value` assignments are
dropped before matching. These ask whatever the rules say:
- command or process substitution (`$(…)`, back quotes, `<(…)`);
- here-documents;
- redirection to a file;
- incomplete quoting.

Redirections that write nowhere (`2>&1`, `>/dev/null`) are fine.

**Reading the screen.** The adapter reads the command from the prompt
(`permission_command`). It returns `None` for anything that is not a shell command, or that
it cannot read with certainty; a person then decides. In v1:
- **Claude Code's Bash prompt:** the command is read. A last line that reads as prose is
  taken for the tool's description. Any other line stays in the command, which makes the
  policy ask.
- **Codex's command approval:** the command is read from its `$ ` line when it fits on that
  line. Codex wraps a long command onto unmarked lines, sometimes inside a word, so a wrapped
  one goes to a person.
- **Edits and other tools, and OpenCode:** they go to a person too.

**Upgrading.** Before 1.0, batond sent every permission prompt to a person. From 1.0 it
answers some itself, so the change is announced in three places:
- `CHANGELOG.md`;
- `baton doctor`, which warns while no `policy.yaml` exists and says what the defaults
  approve and how to turn them off;
- batond's start-up log line "permission policy".

**Acting.** When batond sees a permission prompt (`blocked_permission`), it decides before it
notifies anyone:

1. It records `attempt.permission_decided`: the prompt, the command (secrets masked), the
   decision, the rule and the reason.
2. `allow` or `deny` is then carried out through the operator's own path (`scheduler.operator.act`),
   recorded as `operator.acted` by `policy`.
   - The agent's keys must be known for that prompt (ADR 0009). If they are not, the
     decision becomes `ask`.
   - If the pane cannot be verified, nothing is sent and a person decides.
3. `ask` notifies the operator as before, with the policy's reason in the notice.
4. After a `deny`, the agent stops and waits; the operator is asked what it should do
   instead, and told the policy refused.

Each prompt is decided once. A prompt is told apart from the next by its text. If the
screen shows a different prompt while herdr's state has not changed, that prompt is
observed afresh and decided on its own. A screen with no prompt on it is the same prompt
still being cleared.

`[policy] enabled = false` turns all of this off.

### Consequences

- Good: the prompts that make up most of a task no longer wait for a person; the risky
  ones still do, with the reason in front of the operator.
- Good: the event log shows every automatic answer, with the rule, next to the operator's.
- Good: the policy is plain data with a CLI to try it, so a user can see why a command was
  allowed before it runs.
- Bad: an unattended run in a directory outside `trusted_dirs` still stops at the first
  test run. That is deliberate: trusting a repository's code is a decision for the user,
  made once per repository.
- Bad: inside `trusted_dirs`, a test run can do anything the repository's code does. The
  list should hold only repositories whose contents the user controls.
- Bad: "read-only" is about the command, not every configuration. A user's own git
  configuration (an external diff, a pager) can make `git diff` run a program. A cloned
  repository cannot set that, since `.git/config` is not cloned.
- Bad: `cat` and `grep` may read secrets in the working tree, since reading is allowed.
  Sending them anywhere still needs the network, which asks.
- Bad: matching text is not understanding a shell. Aliases, functions, scripts in the
  repository (`./run.sh`) and interpreters (`python -c`) match no default rule and ask; a
  user rule that allows them allows whatever they do.
- Bad: Claude Code's prompt does not mark where a command ends. A command of several lines
  without a description, whose last line reads as prose, would lose that line. The prose
  test (a capitalized word, then words without shell characters) makes this unlikely, not
  impossible.

## Pros and Cons of the Options

### 1. Every prompt to a person

- Good: nothing runs that a person did not see.
- Bad: unattended runs stall on harmless prompts, and reflex approvals are the real risk.

### 2. The agents' own auto-approve modes

- Good: no screen reading.
- Bad: one switch per agent, each with its own syntax and defaults, outside the event log.
- Bad: the broad modes approve everything; the narrow allow lists live in each agent's
  settings, where baton cannot show or test them.

### 3. A baton policy

- Good: one rule set for every agent, decided where the decision is recorded.
- Bad: baton has to read commands from screens, which is only as good as the adapter.
