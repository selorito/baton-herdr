# The assistant starts work in an existing directory

Branch `feat/assistant-existing-dir`, from `feat/herdr-wsl1`. ADR 0021 amended.

## What changed

- "~/dev/baton-projects/wsl1-smoke içinde calc.py'ye bölme ekle" now gets a card: 🛠 İş
  önerisi, with the title (the request's first sentence), the instructions, the directory
  and why it is allowed. Start adds one task there, as `baton task add` does. There is no
  plan step.
- `assistant/places.py`, `Places.find`, decides whether a directory gets a card: it
  must exist and be inside the policy's `trusted_dirs` or inside a project baton made. A
  project id (`p-1a2b3c`) stands for its project's directory. Links are followed before
  the check. Anything else is refused with the reason: relative path, no such directory,
  not trusted, unknown project.
- `core.assistant.Directories` is the file-system port (`FakeDirectories` in the fakes,
  `LocalDirectories` for real). It only resolves; nothing creates, moves or deletes.
- Start checks the directory again (it may have gone while the card waited). Edit keeps
  the card's directory in front of the model.
- `add_task` moved from `daemon` to `scheduler.operator`. The bot calls it, and `daemon`
  imports the bot, so it could not stay there.
- `[tiers] roles.task` gives the card's tier, unless the request carries `#economy`.
- The intent schema did not change.

## Checks

- `just check-py`: ruff, format, mypy and import-linter are green, and 926 tests pass.
  Two tests fail, `test_classify_golden.py` (`test_every_case_is_answered_as_recorded`,
  `test_every_rule_mutant_changes_some_answer`), and they fail the same way on
  `feat/herdr-wsl1` with this change stashed. The installed `~/.local/bin/baton-detect`
  says `unknown time zone "US/Eastern"` on this machine. Rust is untouched, and this
  WSL1 machine has no Rust toolchain to rebuild it.
- `just smoke` was not run: nothing herdr-related changed.
- Not tried against a live Ollama model: whether `qwen3.5:4b` puts the path into
  `target.path` reliably is still to be measured.
