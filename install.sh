#!/usr/bin/env bash
# Installs baton on Linux, WSL or macOS, for the current user only.
#
#   bash install.sh            # asks before each step
#   bash install.sh --yes      # no questions; the same steps
#   bash install.sh --yes --no-setup   # the same, without step 4
#
# Without a terminal (run by a program), every question is answered "no", so nothing is
# installed without --yes, and step 4, which asks for your bot token, is never run.
#
# What it does, in order. Each step is skipped when its tool is already there.
#   1. uv (Python's package manager), with its official installer, into ~/.local/bin.
#   2. herdr (the terminal multiplexer the agents run in), version $HERDR_VERSION, into
#      ~/.local/bin, checked against its known SHA-256.
#   3. baton itself, with uv, on Python 3.12, into ~/.local/bin, its wheel checked against
#      its known SHA-256.
#   4. `baton setup`, which asks for your Telegram bot and writes ~/.config/baton.
#
# What it never does: use sudo, install system packages, change files outside your home
# directory, delete anything, or install the coding agents (Claude Code, Codex, OpenCode).
# Those you install and sign in to yourself; the end of this script says how.
set -euo pipefail

BATON_VERSION="${BATON_VERSION:-1.4.0}"
HERDR_VERSION="0.9.5"
# Where baton comes from. BATON_SOURCE names another wheel or a checkout (tests use one),
# installed unchecked.
BATON_RELEASE="https://github.com/selorito/baton-herdr/releases/download/v${BATON_VERSION}"
BATON_WHEEL="baton_herdr-${BATON_VERSION}-py3-none-any.whl"
# The wheel's SHA-256 for the versions this script knows; another version is checked
# against its release's SHA256SUMS instead.
baton_sha() {
    case "$1" in
        1.4.0) echo 1599b7139daf44845157d30f62bdab374a518bf0e17543af32ecf6cf10d63481 ;;
    esac
}
BIN="$HOME/.local/bin"
YES=0
SETUP=1
for arg in "$@"; do
    case "$arg" in
        --yes|-y) YES=1 ;;
        --no-setup) SETUP=0 ;;
        -h|--help) sed -n '2,23p' "$0"; exit 0 ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
note() { printf '    %s\n' "$*"; }
fail() { printf '\n\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }
# A terminal to ask in. Run by a program (a coding agent, CI) there is none: questions
# are answered "no", and --yes is the way to accept every step.
if { : </dev/tty; } 2>/dev/null; then TTY=1; else TTY=0; fi
ask() {
    [ "$YES" = 1 ] && return 0
    if [ "$TTY" = 0 ]; then
        note "$1 -> no (no terminal to ask in; run with --yes to accept every step)"
        return 1
    fi
    local answer
    read -r -p "    $1 [Y/n] " answer </dev/tty || answer=n
    case "$answer" in ""|y|Y|yes|evet|e) return 0 ;; *) return 1 ;; esac
}

# --- What this machine is ------------------------------------------------------------
case "$(uname -s)" in
    Linux) system=linux ;;
    Darwin) system=macos ;;
    *) fail "this script is for Linux, WSL and macOS; on Windows use install.ps1" ;;
esac
case "$(uname -m)" in
    x86_64|amd64) machine=x86_64 ;;
    aarch64|arm64) machine=aarch64 ;;
    *) fail "unsupported processor $(uname -m): baton is built for x86_64 and aarch64" ;;
esac
if [ "$system" = linux ] && ldd --version 2>&1 | grep -qi musl; then
    note "musl-based Linux (Alpine): not tested; baton's binaries are static and should run"
fi
if command -v curl >/dev/null 2>&1; then
    fetch() { curl -fsSL --retry 3 -o "$2" "$1"; }
elif command -v wget >/dev/null 2>&1; then
    fetch() { wget -q -O "$2" "$1"; }
else
    fail "neither curl nor wget is installed; install one (e.g. sudo apt install curl)"
fi
if command -v sha256sum >/dev/null 2>&1; then
    sha256() { sha256sum "$1" | cut -d' ' -f1; }
else
    sha256() { shasum -a 256 "$1" | cut -d' ' -f1; }
fi
say "baton $BATON_VERSION for $system $machine"
note "Everything goes under $HOME; nothing needs sudo."

mkdir -p "$BIN"
export PATH="$BIN:$PATH"

# --- 1. uv ---------------------------------------------------------------------------
if command -v uv >/dev/null 2>&1; then
    say "uv: found $(command -v uv)"
else
    say "uv is missing"
    note "It is installed by its official installer (https://docs.astral.sh/uv/), which"
    note "puts uv in ~/.local/bin and adds that directory to your shell's PATH."
    ask "Install uv now?" || fail "uv is needed; install it and run this again"
    installer="$(mktemp)"
    fetch https://astral.sh/uv/install.sh "$installer"
    UV_INSTALL_DIR="$BIN" sh "$installer"
    rm -f "$installer"
    command -v uv >/dev/null 2>&1 || fail "uv did not install; see the output above"
fi

# --- 2. herdr ------------------------------------------------------------------------
herdr_sha() {
    case "$1" in
        linux-x86_64) echo 14d9e8bc8b4a9e0118d77c32266f76d461f09b40342eb23752960bd89f59929d ;;
        linux-aarch64) echo 17d7536f2da50859523680ef279dffcac8e4a235f4e46fe8da6c2ec44141897c ;;
        macos-x86_64) echo 5fb0a908ec7f6745e3e245738b06bfad56c46391c391ab3c0955fbead01f1369 ;;
        macos-aarch64) echo fb77c2232a05a8035421382c894392f4174e4e38a18e48af3dd0290157c3a7e2 ;;
    esac
}
if command -v herdr >/dev/null 2>&1; then
    say "herdr: found $(command -v herdr) ($(herdr --version 2>/dev/null || echo 'version unknown'))"
else
    asset="herdr-$system-$machine"
    url="https://github.com/herdrdev/herdr/releases/download/v$HERDR_VERSION/$asset"
    say "herdr is missing"
    note "Download: $url"
    note "Into: $BIN/herdr (SHA-256 checked)"
    ask "Install herdr $HERDR_VERSION now?" || fail "herdr is needed; install it and run this again"
    part="$BIN/.herdr.part"
    fetch "$url" "$part"
    if [ "$(sha256 "$part")" != "$(herdr_sha "$system-$machine")" ]; then
        rm -f "$part"
        fail "the herdr download does not match its known checksum; nothing was installed"
    fi
    chmod 755 "$part"
    if [ "$system" = macos ]; then
        xattr -d com.apple.quarantine "$part" 2>/dev/null || true
    fi
    mv -f "$part" "$BIN/herdr"
    note "Installed $BIN/herdr"
fi

# --- 3. baton ------------------------------------------------------------------------
if command -v baton >/dev/null 2>&1 && [ "$(baton version 2>/dev/null)" = "baton $BATON_VERSION" ]; then
    say "baton: found $(command -v baton) ($BATON_VERSION)"
else
    say "baton $BATON_VERSION"
    source="${BATON_SOURCE:-$BATON_RELEASE/$BATON_WHEEL}"
    note "From: $source"
    [ -n "${BATON_SOURCE:-}" ] || note "SHA-256 checked before it is installed"
    note "With: uv tool install --python 3.12 (uv downloads Python 3.12 if it is missing)"
    ask "Install baton now?" || fail "nothing more to do without baton"
    if [ -z "${BATON_SOURCE:-}" ]; then
        wheel_dir="$(mktemp -d)"
        fetch "$source" "$wheel_dir/$BATON_WHEEL"
        expected="$(baton_sha "$BATON_VERSION")"
        if [ -z "$expected" ]; then
            fetch "$BATON_RELEASE/SHA256SUMS" "$wheel_dir/SHA256SUMS"
            expected="$(awk -v f="$BATON_WHEEL" '$2 == f || $2 == "*" f { print $1 }' "$wheel_dir/SHA256SUMS")"
        fi
        if [ -z "$expected" ] || [ "$(sha256 "$wheel_dir/$BATON_WHEEL")" != "$expected" ]; then
            rm -rf "$wheel_dir"
            fail "the baton download does not match its checksum; nothing was installed"
        fi
        source="$wheel_dir/$BATON_WHEEL"
    fi
    uv tool install --force --python 3.12 "$source"
    [ -z "${wheel_dir:-}" ] || rm -rf "$wheel_dir"
fi

# --- What is left --------------------------------------------------------------------
missing=()
for tool in git claude codex opencode; do
    command -v "$tool" >/dev/null 2>&1 || missing+=("$tool")
done
say "Coding agents"
note "baton drives the agents you install and sign in to yourself; at least one is needed."
for tool in claude codex opencode; do
    if command -v "$tool" >/dev/null 2>&1; then note "$tool: $(command -v "$tool")"; fi
done
case " ${missing[*]} " in *" claude "*) note "Claude Code: curl -fsSL https://claude.ai/install.sh | bash   then: claude (sign in)" ;; esac
case " ${missing[*]} " in *" codex "*) note "Codex CLI:   npm install -g @openai/codex   then: codex (sign in)" ;; esac
case " ${missing[*]} " in *" git "*)
    if [ "$system" = macos ]; then note "git: xcode-select --install"; else note "git: install it with your package manager (sudo apt install git)"; fi ;;
esac

if [ "$SETUP" = 1 ] && [ "$TTY" = 0 ]; then
    say "baton setup"
    note "It asks for your Telegram bot token, so it needs you at a terminal; it was not"
    note "run. Next, in a terminal: baton setup"
elif [ "$SETUP" = 1 ]; then
    say "baton setup"
    note "It finds your agents, sets up Telegram (you will need a bot token from"
    note "@BotFather), writes ~/.config/baton, and installs the service. Safe to run again."
    if ask "Run baton setup now?"; then
        baton setup
    else
        note "Later: baton setup"
    fi
fi

if ! grep -qs '.local/bin' "$HOME/.profile" "$HOME/.bashrc" "$HOME/.zshrc" 2>/dev/null; then
    say "PATH"
    note "Add ~/.local/bin to your PATH, e.g. in ~/.profile or ~/.zshrc:"
    note '  export PATH="$HOME/.local/bin:$PATH"'
fi
say "Done. Check everything with: baton doctor"
