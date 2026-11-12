---
status: accepted
date: 2026-11-12
---

# Publishing delivered pages on Vercel, for anyone to open

## Context and Problem Statement

A delivered project's page is served by the read-only preview (ADR 0016), on the networks
baton's machine is on. Someone on another network cannot open it: the operator on mobile
data, or the people a site is made for. The operator demonstrates baton by having a
request built on the spot ("make another one") and handing over the link; that needs a
link anyone opens, every time, without setting up hosting by hand.

## Decision Drivers

- Off unless the operator turns it on: publishing makes an agent's work public.
- Nothing that is not the page leaves the machine: not the repository, `.baton`, or `.env`.
- No new runtime on the server (WSL1 has no Node of its own) and no new Python dependency.
- A page that cannot be published is still delivered; the notice says why.

## Considered Options

1. Expose the preview to the internet (port forwarding, a tunnel).
2. Vercel's CLI, run from batond.
3. Vercel's REST API, from batond: upload the page's files, create a production deployment.

## Decision Outcome

Chosen option: **3**, behind `[projects] publish`: `"off"` (default) or `"vercel"`, with
the token in `.env` (`BATON_PROJECTS__VERCEL_TOKEN`, a `SecretStr`) and an optional team.

- **What is sent.** The page's directory as the preview finds it (`find_page`: the
  project, `dist`, `build`, `public`, `site`), by the preview's rules: files only, nothing
  under a hidden name, nothing a link points to outside it; at most 2000 files and 50 MB.
- **How.** Each file is uploaded to `/v2/files` by its SHA-1; `/v13/deployments` then
  creates a production deployment naming them, with no framework and no build, for a
  Vercel project named after the project's directory (already a unique slug). baton polls
  until it is `READY` and has its aliases.
- **The link.** The shortest production alias, the project's own `<name>.vercel.app`.
  A deployment's own URL is behind Vercel's login by default; the production domain is not.
- **When.** At delivery, after the preview link and picture; the event `project.delivered`
  records `public_url`. A failure is logged and the notice says "Not published: …".
  `baton project publish p-…` publishes any project now, whatever the setting says.

### Consequences

- Good: a demo's site opens on anyone's phone a minute after its last step.
- Good: no Node, no CLI, no tunnel; the preview stays on the local networks.
- Bad: a public page is public. What an agent wrote in it (names, a notification topic)
  can be read by anyone; the operator turns publishing on knowing that.
- Bad: one Vercel project per baton project accumulates on the account; they are removed
  on Vercel, not by baton.
- Neutral: pages with a server side (Vercel functions) are out of scope; static files only.
