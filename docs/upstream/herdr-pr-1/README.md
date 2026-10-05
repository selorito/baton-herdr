# herdr upstream contribution: findings, and why there is no patch

Prepared 2026-10-05 from baton's recorded screens (`fixtures/codex/`, herdr 0.9.1, Codex CLI
0.156.1). The plan was a pull request against herdr's agent-detection manifests. This file
records what was found and why it ends as an issue comment instead.

## herdr's rules decide the route

herdr's [CONTRIBUTING.md](https://github.com/herdrdev/herdr/blob/master/CONTRIBUTING.md)
and `AGENTS.md`, as of 2026-10-05, say:

- **Who may open pull requests:** only maintainers and accounts listed in
  `.github/APPROVED_CONTRIBUTORS`. Pull requests from anyone else are closed
  automatically, whatever their quality. The list is curated by the maintainers; asking to
  be added is not allowed.
- **No ready-made patches:** agents must not prepare or submit completed patches, or file
  issues to justify code already written.
- **What to do instead:**
  - a reproducible bug goes in a short issue on the bug template, without root-cause
    analysis or a proposed fix;
  - ideas go to Discussions.

The account `selorito` is not in `APPROVED_CONTRIBUTORS`. A pull request would be closed
unread, so no patch was prepared.

## What baton's recordings show, against herdr today

| Gap in herdr 0.9.1 (recorded) | herdr `master` now | Upstream tracking |
|---|---|---|
| Codex 0.156.1's folder-trust dialog ("Folder access / Trust this folder? / › 1. Trust and continue") is reported `idle` (`default_known_agent_idle_fallback`), so a prompt sent to it answers the dialog. Recording: `fixtures/codex/blocked_permission/20260930T075506Z`. | **Fixed.** `trust_directory` now accepts "Folder access" and "Trust this folder?". | [#4343](https://github.com/herdrdev/herdr/issues/4343) (open), [#4458](https://github.com/herdrdev/herdr/discussions/4458) |
| Codex's startup update chooser ("Update available · 0.156.1 → 0.159.2 … enter continue · esc skip") is reported `idle`. The `startup_update` rule still requires "Update available!" and "Press enter to continue", which this wording does not have. Recording: `fixtures/codex/blocked_question/20260930T075645Z`. | **Not fixed.** | [#4811](https://github.com/herdrdev/herdr/issues/4811) (open): the same chooser, reported on Codex 0.159 |
| No positive idle rule for Codex (idle is always a fallback). | Addressed as a bug | [#4778](https://github.com/herdrdev/herdr/issues/4778) (closed) |

The update chooser is the clearest open gap. It is already reported, so the only new fact
baton has is that the wording, and the miss, go back to Codex 0.156.1 at least. #4811 shows
it on 0.159. Note what herdr reports for the chooser:
- on 0.156.1 with manifest 2026.09.23.1 it reported `idle`;
- in #4811 it reported `unknown` (`codex_state_ambiguous`), probably because Codex 0.159
  changed what the rest of the screen looks like.

## The allowed contribution: one comment on #4811

Paste this as a comment on <https://github.com/herdrdev/herdr/issues/4811>. It states
facts from the recording, with no analysis or fix, as CONTRIBUTING asks:

```markdown
Also reproduced with Codex CLI 0.156.1 on herdr 0.9.1 (stable, Linux), manifest
2026.09.23.1 (`remote_update_status: current`). Same chooser wording:

    Update available · 0.156.1 → 0.159.2
    › 1. Update now (runs `npm install -g @openai/codex`)
      2. Skip
      3. Skip until next version
      enter continue · esc skip

`herdr agent explain <pane> --json` gave `state: idle`, `matched_rule: null`,
`fallback_reason: default_known_agent_idle_fallback`, `visible_blocker: false`, and the pane
reported `agent_status: idle`. So on 0.156.1 the chooser reads as idle rather than unknown,
and a prompt sent with `herdr agent prompt` reaches the dialog.
```

Before posting:

1. Reproduce it once more. Start Codex 0.156.1 (or any version that shows this chooser)
   in a herdr 0.9.1 pane, then run `herdr agent explain <pane> --json`. The rules require
   that you have reproduced it yourself; the recording is from 2026-09-30.
2. Check that #4811 is still open and nobody has already added 0.156.1.
3. Answer any follow-up from herdr's issue agent with the one detail it asks for.

## If your account is ever on APPROVED_CONTRIBUTORS

Only then would a pull request be in order, and the maintainers decide that, not a
request. The repository's own rules (`AGENTS.md`, "Agent Detection Updates") would then
apply:

1. Fork `herdrdev/herdr` and create a branch: `gh repo fork herdrdev/herdr --clone`, then
   `git switch -c fix/codex-update-chooser`.
2. Capture the live state with
   `herdr agent read <pane> --source detection --format text`, and check the matching with
   `herdr agent explain <pane> --json`.
3. Edit `src/detect/manifests/codex.toml`. Copy it to
   `~/.config/herdr/agent-detection/codex.toml`, first checking that no override already
   exists. Then run `herdr server reload-agent-manifests` and confirm the state live.
4. Tests:
   - engine tests use only synthetic manifests;
   - herdr asks for no tests that classify recorded CLI screens against the bundled rules;
   - keep `distribution/agent-detection/codex.toml` aligned with the bundled manifest.
5. Run `just ci`. Use a lowercase conventional title (`fix: …`) and `refs #4811` in the
   commit body, never `fixes`.
6. Remove the local override afterwards.
