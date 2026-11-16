#!/usr/bin/env bash
# A clean machine, end to end, inside a container: install.sh, baton doctor, and one task
# queued and completed by an agent in herdr. Run by tools/e2e/run.sh, one distribution each.
#
# The agent is tools/fake-agent playing Codex (it does the task and says it is done), so no
# account or quota is needed. Everything else is what a buyer gets: install.sh, herdr from
# its release, baton from a wheel, baton-detect, the daemon's runner and herdr's panes.
#
# Mounted: /repo (the checkout, read-only), /dist (the wheel and baton-detect).
set -euo pipefail
distro=$1
log() { printf '\n##### %s\n' "$*"; }

log "$distro: base packages (what a minimal image lacks: curl, git)"
case "$distro" in
    ubuntu*|debian*) export DEBIAN_FRONTEND=noninteractive
        apt-get update -qq >/dev/null && apt-get install -y -qq curl ca-certificates git procps >/dev/null ;;
    fedora*) dnf install -y -q curl git procps-ng >/dev/null ;;
    arch*) pacman -Syu --noconfirm --quiet curl git procps-ng >/dev/null 2>&1 ;;  # never -Sy alone
esac
useradd -m buyer
cp /dist/baton-detect /usr/local/bin/baton-detect-dist

su - buyer -c "bash -s" <<'BUYER'
set -euo pipefail
log() { printf '\n##### %s\n' "$*"; }
export LANG=C.UTF-8

log "install.sh --yes"
wheel=$(ls /dist/baton_herdr-*.whl)
version=$(basename "$wheel" | cut -d- -f2)
BATON_VERSION="$version" BATON_SOURCE="$wheel" bash /repo/install.sh --yes --no-setup
export PATH="$HOME/.local/bin:$PATH"
# baton setup would download baton-detect from the release; the release is not out yet,
# so the binary built for it is put where setup puts it.
install -m 755 /usr/local/bin/baton-detect-dist "$HOME/.local/bin/baton-detect"

log "versions"
baton version; herdr --version; baton-detect --version; git --version

log "baton doctor (nothing configured yet)"
baton doctor || true

log "end to end: one task, queued and completed in herdr"
work=$HOME/e2e; mkdir -p "$work/calc" "$work/agents"
printf 'def add(a, b):\n    return a + b\n' > "$work/calc/calc.py"
git -C "$work/calc" init -q -b main
git -C "$work/calc" -c user.name=e2e -c user.email=e2e@example.invalid add .
git -C "$work/calc" -c user.name=e2e -c user.email=e2e@example.invalid commit -qm start
uv run --quiet --no-project --python 3.12 /repo/tools/fake-agent/fake_agent.py --make-launcher "$work/agents" codex >/dev/null
cat > "$work/baton.toml" <<TOML
[herdr]
session = "e2e"
[database]
path = "$work/baton.db"
[scheduler]
agents = ["codex"]
timezone = "UTC"
poll_interval_seconds = 0.5
[scheduler.launch_commands]
codex = "env TZ=UTC $work/agents/codex /repo/tools/fake-agent/scripts/codex-finish.toml"
[usage]
enabled = false
[policy]
enabled = false
TOML
export BATON_CONFIG="$work/baton.toml" HERDR_SESSION=e2e TERM=xterm-256color
herdr --session e2e server >"$work/herdr.log" 2>&1 &
for _ in $(seq 50); do herdr workspace list >/dev/null 2>&1 && break; sleep 0.2; done
baton doctor | grep -E "^(ok|warn|fail) +(system|herdr|herdr server|codex|git|detector|timezone)" || true
baton task add "power function" -C "$work/calc" -i "Add power(a, b) to calc.py."
timeout 300 baton run
baton status
baton status | grep -q completed && echo "E2E RESULT: task completed" || { echo "E2E RESULT: FAILED"; exit 1; }
herdr server stop >/dev/null 2>&1 || true
BUYER
