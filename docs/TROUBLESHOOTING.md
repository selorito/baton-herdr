# Troubleshooting

Start with:

```bash
baton doctor
```

Every line is `ok`, `warn` or `FAIL`, and a line that is not `ok` is followed by `->` and
what to do. Fix every `FAIL`. A `warn` is something baton can run without. The entries
below are grouped by system. Each says what you see, why it happens, and how to fix it.
Many of them are problems found and fixed while preparing 1.4.0. If you run an older
baton, updating is the fix (see [INSTALL](INSTALL.md#updating-and-removing)).

Where the logs are:

| System | batond and herdr server |
|--------|-------------------------|
| Linux, WSL2 (systemd) | `journalctl --user -u baton.service -f`, `journalctl --user -u baton-herdr.service` |
| WSL1 | `~/.local/state/baton/` (`daemon.log`, `herdr-server.log`, `wsl1-start.log`) |
| macOS (launchd) | `~/Library/Logs/baton/` (`daemon.log`, `herdr-server.log`) |
| Windows, native | `%USERPROFILE%\.local\state\baton\` (`daemon.log`, `herdr-server.log`, `windows-start.log`, and an `.err.log` for each) |

`baton service status` says what is installed and whether both programs are running.

## Every system

### herdr's version

- **Symptom.** `FAIL herdr ... older than 0.9.1`, or `warn herdr ... newer than baton was
  tested with (0.9.5)`.
- **Cause.** baton talks to herdr's socket API, and only herdr 0.9.1 to 0.9.5 has been
  tried with it. Below 0.9.1 the protocol lacks calls baton uses. Above 0.9.5 nothing is
  known to break, but nothing has been checked either.
- **Fix.**
  - Below the range: `herdr update`, or run the install script again. It installs
    0.9.5.
  - Above it: this is usually fine. If panes misbehave, install herdr 0.9.5 again with
    the install script and report the newer version together with your `baton doctor`
    output.

### baton-detect missing, wrong version, or not running

- **Symptom.**
  - `FAIL baton-detect ... not found`, `FAIL detector ... not found`.
  - `warn baton-detect ... but baton is 1.4.0`.
  - `FAIL detector ... misreads a test screen`.
- **Cause.** baton reads agents' screens (limits, questions, permission prompts) with
  `baton-detect`. It must be the build for your system, and of the same version as
  baton. After a baton update, the old binary stays in place.
- **Fix.** Delete it and run setup, which downloads the matching one and checks its
  SHA-256:
  ```bash
  rm ~/.local/bin/baton-detect && baton setup
  ```
  If `system` says `no baton-detect is released for this processor`, only building it
  from a checkout works: `cargo install --path crates/baton-detect --locked`.

### The config file

- **Symptom.** baton (any command) prints one line: `.../baton.toml: not valid TOML (...)`.
  Or `doctor` shows `FAIL config .../baton.toml: <setting>: <message>`. Before 1.4.0, a
  Python traceback that ends in `TOMLDecodeError`.
- **Cause.** A typo in the file, or a value of the wrong kind. Before 1.4.0 this also
  happened to a file saved *with a byte order mark*, which Windows PowerShell 5.1
  (`Set-Content -Encoding utf8`) and older Notepad add. 1.4.0 reads such files.
- **Fix.** The message names the line or the setting. Compare it with
  `baton.example.toml`. To look for a BOM on Linux or macOS: `head -c3 ~/.config/baton/baton.toml | xxd`
  (it is one if the output shows `efbb bf`).

### The project preview port is taken

- **Symptom.** A delivered project's link points to `:8766` or another port, not
  `:8765`. Before 1.4.0, previews were simply not served, and `doctor` named no cause.
- **Cause.** Another program, often a second user's batond on the same machine, holds
  `[projects] preview_port` (8765).
- **Fix.** Nothing is needed: baton tries the next nine ports and links the one it got.
  To pick a port yourself, set `[projects] preview_port`. Two users on one machine also
  need **one Telegram bot each**: Telegram lets only one program read a bot's messages,
  so two batonds with one token take each other's messages.

### Proxy

- **Symptom.** `FAIL telegram bot not reachable (...)` on a company or university
  network, while `baton setup` reached Telegram fine. The bot never answers.
- **Cause.** Before 1.4.0 the bot ignored proxy settings. Setup and Vercel publishing used
  a library that reads them, so the failure was only partial.
- **Fix.** In 1.4.0 the bot uses `HTTPS_PROXY` (or `ALL_PROXY`), unless `NO_PROXY` names
  `api.telegram.org`. For the service, set the proxy in the config, not only in your
  shell:
  ```toml
  [telegram]
  proxy = "http://proxy.example:3128"   # "" turns the proxy off
  ```
  When it works, `doctor` shows `ok telegram @yourbot, ... through http://proxy.example:3128`.
  Credentials in the URL are not printed. Only HTTP(S) proxies have been tested.

### herdr integrations

- **Symptom.** `FAIL claude integration` (or codex, opencode) with a status other than
  `current`.
- **Cause.** herdr's hook for that agent is not installed, so baton cannot learn the
  agent's session id. Without the id it can neither resume nor verify that agent's
  sessions.
- **Fix.** `herdr integration install claude` (or the agent `doctor` names). `baton setup`
  does this for every agent it finds.

### Other doctor failures

| doctor says | Cause | Fix |
|-------------|-------|-----|
| `FAIL herdr herdr is not on PATH` | herdr is not installed, or the terminal predates the install | Run the install script again, or open a new terminal |
| `FAIL herdr server not reachable` | The herdr server is not running | Run `baton setup` again and let its last step install the service (`baton service install` alone only writes the files), or start it as the hint says (WSL1: see below) |
| `FAIL service session ...` | The CLI looks at a different herdr session than the service runs | Do what the hint says; often it is running baton inside a herdr pane |
| `FAIL git ...` | git is not installed | Linux: your package manager; macOS: `xcode-select --install`; Windows: Git for Windows |
| `FAIL timezone unknown zone` | The name is not an IANA zone | `[scheduler] timezone = "Europe/Istanbul"` (an IANA name) |
| `warn timezone ..., but this machine is on ...` | Agents print reset times in local time | Set `[scheduler] timezone` to the zone shown |
| `FAIL policy ...` | `policy.yaml` is broken; batond will not start | Fix the line named; the format is in `policy.example.yaml` |
| `FAIL database ... cannot be created` | The data directory is not writable | Make `~/.local/share/baton` writable, or set `[database] path` |
| `warn screenshots no Chromium or Chrome found` | Deliveries come without a picture | Install Chrome or Chromium, or set `[projects] browser` |
| `warn assistant ...` | Ollama is not running, or lacks the model | `ollama serve`, then `ollama pull <model>`; the bot works without it |

## Linux and WSL2

- **Symptom.** `baton setup`'s service step fails with `systemctl: not found` or `System has
  not been booted with systemd`.
- **Cause.** WSL2 without systemd enabled, or a minimal container.
- **Fix.** On WSL2, add the following to `/etc/wsl.conf`, then run `wsl --shutdown` in
  PowerShell and open Ubuntu again:
  ```ini
  [boot]
  systemd=true
  ```
  On WSL1, see the next section. WSL2 has not been tried end to end by us.

## WSL1

`doctor` shows a `wsl1` line when it detects WSL1.

### An agent's pane closes right after it starts

- **Symptom.** A task's agent starts, and its pane hangs up or disappears while the
  instructions are pasted. This happens most with project steps, whose prompts (the
  request, the plan and the step) are around 10 KB. The task then fails or is retried.
- **Cause.** WSL1's terminal buffer is small. A paste of more than about 4 KB, or pieces
  sent back to back while the agent is still starting, overflows it, and the terminal
  hangs up.
- **Fix.** Update to 1.4.0. It pastes in 1 KiB pieces with a 0.25 s pause between them.

### No agent is ever seen to start

- **Symptom.** `warn wsl1 ... process_detection = "herdr"`. Tasks stay at "starting".
- **Cause.** herdr cannot see a pane's processes on WSL1.
- **Fix.** Set `[herdr] process_detection = "auto"`, or remove the setting. baton then
  reads the panes itself.

### The service does not start after a reboot

- **Symptom.** `FAIL herdr server not reachable` after Windows restarts.
- **Cause.** WSL1 has no systemd. A Windows scheduled task has to start baton, and it
  needs administrator rights to register, which baton does not use.
- **Fix.** `baton service install` prints the PowerShell command that registers the task.
  Run that command yourself, as administrator. To start the task now, run
  `Start-ScheduledTask -TaskName 'baton'`. Its log is `~/.local/state/baton/wsl1-start.log`.

### The logs fill the disk

- **Symptom.** `~/.local/state/baton/daemon.log` grows to hundreds of MB. On one machine
  it reached 343 MB in four days before 1.4.0.
- **Cause.** Without systemd, nothing rotated the start script's logs.
- **Fix.** Update to 1.4.0 and run `baton service install --force`, which rewrites the
  start script, then restart the scheduled task. It then moves a log over 20 MB to `<name>.1` and empties it in place. Until
  then, you can empty a log safely with `: > ~/.local/state/baton/daemon.log`.

## macOS

macOS support is **beta**: it is built and tested by CI, but has not been run on a real
Mac by us.

- **Symptom.** "baton-detect cannot be opened because the developer cannot be verified",
  or "Apple could not verify ...".
- **Cause.** Gatekeeper blocks files that a browser downloaded (they carry a quarantine
  flag) and that are not notarized. baton's binaries are not notarized. `baton setup` and
  `install.sh` download without that flag, so this only happens to a copy you fetched
  with a browser.
- **Fix.** Remove the flag from that one file, or delete it and let `baton setup` download
  it:
  ```bash
  xattr -d com.apple.quarantine ~/.local/bin/baton-detect
  ```
  The same applies to herdr if you downloaded it by hand.

---

- **Symptom.** `FAIL git ...` or `xcrun: error: invalid active developer path`.
- **Cause.** The command line tools are not installed, or a macOS update removed them.
- **Fix.** `xcode-select --install`.

---

- **Symptom.** The service runs, but an agent's pane says the agent is not found. For
  example, an npm-installed Codex in a custom prefix.
- **Cause.** The launchd agents' PATH is `/opt/homebrew/bin`, the system's directories,
  and the directories where `baton service install` found herdr, baton and the agents.
  An agent installed or moved later is not on it.
- **Fix.** Run `baton setup` again from a terminal where `which claude` (or codex,
  opencode) works, and answer yes to "The service is installed already. Update it?".

---

- **Symptom.** `herdr server not reachable` while herdr is running, with a long home
  directory or `XDG_CONFIG_HOME`.
- **Cause.** macOS limits a Unix socket's path to 104 bytes. `doctor` does not yet warn
  about this.
- **Fix.** Use a shorter `XDG_CONFIG_HOME`, or set `[herdr] socket_path` to a short path.

## Windows, native

Native Windows is **beta**. baton's test suite and one end-to-end run with a stand-in agent
have passed on Windows 11, but it has not been used day to day, nor with a real agent
signed in. WSL is the proven way on Windows.

- **Symptom.** `FAIL codex C:\...\codex.exe is not on PATH` (or another agent), although
  the file exists. This happened before 1.4.0, with a full path in
  `[scheduler] launch_commands`.
- **Cause.** The command was split by Unix shell rules, which treat `\` as an escape
  character and dropped it.
- **Fix.** Update to 1.4.0, which splits commands by Windows rules there. A path with
  spaces must be quoted: `"C:\Program Files\...\codex.exe"`.

---

- **Symptom.** `install.ps1 cannot be loaded because running scripts is disabled on this
  system`.
- **Cause.** PowerShell's execution policy.
- **Fix.** Run it as INSTALL shows, `powershell -ExecutionPolicy Bypass -File install.ps1`.
  This applies to that one run only.

---

- **Symptom.** `baton`, `herdr` or `baton-detect` is not recognised right after the
  install.
- **Cause.** The installer added `%USERPROFILE%\.local\bin` and herdr's directory to your
  user PATH. A terminal that was already open does not see the change.
- **Fix.** Open a new terminal.

---

- **Symptom.** baton does not start at boot.
- **Cause.** The scheduled task needs administrator rights to register, so setup only
  prints the command for it.
- **Fix.** Run the printed command in PowerShell as administrator. Its log is
  `%USERPROFILE%\.local\state\baton\windows-start.log`.

---

- **Symptom.** Codex asks permission for every command, even `ls`.
- **Cause.** On Windows, Codex runs commands in PowerShell, and the policy's patterns are
  written for Unix commands. Asking is the safe default.
- **Fix.** Approve them from Telegram, or allow the ones you trust in `policy.yaml`
  (rules are tried before the defaults; see `policy.example.yaml`):
  ```yaml
  rules:
    - decision: allow
      command: "Get-ChildItem*"
      agents: [codex]
      reason: lists files, reads only
  ```
  Check a rule before relying on it: `baton policy check "Get-ChildItem src" --agent codex`.

## Questions

### The Telegram bot does not answer

Check these in order:

1. **Is batond running?** The bot runs inside batond, so with batond stopped nothing
   answers. `baton service status`.
2. **`baton doctor`'s `telegram` line.**
   - `not configured`: run `baton setup`. It asks for the token from
     [@BotFather](https://t.me/BotFather) and finds your chat after you write to the bot.
   - `bot not reachable`: the token is wrong, or the network needs a proxy
     ([Proxy](#proxy)).
   - `notices only`: `[telegram] owner_id` is not set. The bot then sends notices but
     takes no orders.
3. **Are you writing from the right account?** The bot obeys only the chat in
   `[telegram] chat_id` and the user in `owner_id`. It ignores everyone else.
4. **Is the bot running twice?** A second batond with the same token (another user,
   another machine, an old service) takes the messages. Use one bot per baton.
5. **A plain sentence gets "the assistant did not answer".** The optional assistant
   (Ollama) is off or not running. Commands (`/status`, `/new`, `/cancel`, `/tier`) work
   without it.

### An agent is not found

- `doctor` shows `FAIL claude claude is not on PATH`, or the same for codex or opencode.
- Install the agent and sign in to it once yourself ([INSTALL](INSTALL.md#the-agents)).
  Then run `baton setup` again. Any agent you do not use should be removed from
  `[scheduler] agents`.
- If the terminal finds the agent but the service does not, the service has an older
  PATH. Run `baton setup` again from that terminal and let it update the service.

### Permission denied for a binary

- **Symptom.** `permission denied: baton-detect`, or `doctor` shows `baton-detect ... does
  not run (PermissionError)`.
- **Cause.** The file is not executable. This happens when it was copied by hand.
  Another cause is a home directory mounted `noexec`.
- **Fix.**
  - Run `chmod +x ~/.local/bin/baton-detect`, or delete the file and run `baton setup`.
  - With `noexec`, set `[usage] binary` and `[detector] binary` to a copy on a filesystem
    that allows running programs.
  - `exec format error` or `bad CPU type` means the build is for another system. Delete
    it and run `baton setup`.

### macOS blocked the program

This is Gatekeeper's quarantine on a browser download; see [macOS](#macos). Remove the
flag with `xattr -d com.apple.quarantine <file>`, or let `baton setup` download the file
again. baton never asks you to turn Gatekeeper off.
