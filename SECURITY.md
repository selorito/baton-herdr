# Security

baton types into terminals that run coding agents with access to your code and shell, and it
can be driven from Telegram. Please report vulnerabilities privately, through
[GitHub's private vulnerability reporting](https://github.com/selorito/baton-herdr/security/advisories/new),
not in a public issue.

Especially relevant:

- anything that lets someone other than the configured Telegram owner act on a pane, or an
  action land on a prompt other than the one it was shown for
  ([ADR 0009](docs/adr/0009-remote-operator-actions.md));
- input reaching a pane that does not host the attempt
  ([ADR 0006](docs/adr/0006-attempt-identity-and-location.md));
- credentials or personal data in logs, the event log or `fixtures/`
  ([ADR 0005](docs/adr/0005-automation-boundaries.md)).

Only the latest commit on `main` is supported.
