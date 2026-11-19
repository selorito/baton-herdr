# Listing text (baton 1.4.0)

## Title

baton: run your coding agents from Telegram

## Summary

baton keeps Claude Code, Codex CLI and OpenCode working through your task queue on your
own machine, and lets you start, approve, retarget and stop their work from Telegram.

## Description

Queue a task, walk away, and manage the rest from your phone. baton runs
your coding agents inside herdr panes on your own computer. It hands each task to the
agent that still has quota, and when an agent hits its usage limit, baton resumes the
task after the reset. It reports to you in Telegram as the work goes.

**From Telegram you can:**

- start a project with `/new a one-page site for my cafe`, then read the agent's plan
  and press Start;
- see what is running with `/status` and what each agent has left with `/budget`;
- approve or deny an agent's permission prompt with one button, or answer its question
  by replying;
- cancel a task (`/cancel t-…`) or move it to the cheaper model (`/tier t-… economy`);
- stop everything at once by writing `stop` (or `dur`, `iptal`);
- write in plain words, such as "cancel the cafe task", if you turn on the optional local
  assistant (Ollama). Each request comes back as a card to confirm.

**On your machine:**

- One install command per system.
- `baton doctor`, which checks everything and says how to fix what it finds.
- A permission policy that lets read-only commands through and asks you about the rest.
- An append-only event log of everything that happened.

The included skill teaches your coding agent to install baton, run `baton doctor`,
connect the bot, queue, cancel and retarget tasks, and look up problems in the bundled
troubleshooting guide. It never asks for or touches your tokens: you type the bot token
into `baton setup` yourself.

**You bring:**

- at least one of Claude Code, Codex CLI or OpenCode, installed and signed in with your
  own account;
- a Telegram bot token from @BotFather;
- git.

baton runs locally. The computer it runs on has to stay on and awake.

- baton uploads none of your code.
- Your agents talk to their providers as they always do.
- Telegram receives baton's notices.
- Publishing finished pages to Vercel is off unless you turn it on.

**Requires:** baton 1.4.0 from github.com/selorito/baton-herdr (tag v1.4.0) and herdr
0.9.1 to 0.9.5. The install script sets up both. Apache-2.0.

## Support

"Supported" means run end to end on a real machine by the maintainer. "Beta" means it is
built and tested automatically, but not yet on a real machine, or not yet with a real
agent.

| System | Status |
|--------|--------|
| Ubuntu (x86_64) | Supported |
| Windows 10/11 with WSL1 | Supported (one machine, unattended since 2026-11-10) |
| Debian 12, Fedora 42, Arch (x86_64) | Beta: clean install passes in containers |
| Windows 10/11 with WSL2 | Beta |
| Windows 10/11, native | Beta: passes on a real machine with a stand-in agent, not yet with a real one |
| macOS, Apple Silicon and Intel | Beta: not yet run on a Mac |
| Linux ARM64 | Beta: not yet run on ARM hardware |
