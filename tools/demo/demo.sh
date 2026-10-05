#!/usr/bin/env bash
# `just demo`: record baton handing a task from Claude Code to Codex, as docs/assets/demo.gif.
#
# Everything is real except the agents. A throwaway herdr session runs batond, the CLI,
# the screen classifier and herdr's panes. The agents are tools/fake-agent playing
# Claude Code (it hits its session limit) and Codex (it finishes), so no quota is spent
# and every run shows the same thing.
#
# The recording is herdr's own terminal client, captured with asciinema and rendered
# with agg. A driver types into the panes and switches tabs while it records.
# DEMO_KEEP=1 keeps the working directory (batond's log, the recording) for a look.
#
# Needs: herdr, uv, cargo, curl. agg is downloaded into ~/.cache/baton-demo unless it is
# on PATH or AGG points at it. Usage: tools/demo/demo.sh [OUTPUT.gif]
set -euo pipefail

REPO=$(cd "$(dirname "$0")/../.." && pwd)
OUT=${1:-$REPO/docs/assets/demo.gif}
COLS=150
ROWS=34
SESSION="baton-demo-$$"
WORK=$(mktemp -d /tmp/baton-demo.XXXXXX)

# Never talk to a herdr session or agent this shell may be running in.
unset HERDR_SOCKET_PATH HERDR_CLIENT_SOCKET_PATH HERDR_ENV HERDR_PANE_ID
export HERDR_SESSION="$SESSION"

say() { printf 'demo: %s\n' "$*" >&2; }

agg_binary() {
    if [[ -n "${AGG:-}" ]]; then echo "$AGG"; return; fi
    if command -v agg >/dev/null; then command -v agg; return; fi
    local cached="${XDG_CACHE_HOME:-$HOME/.cache}/baton-demo/agg"
    if [[ ! -x "$cached" ]]; then
        say "downloading agg 1.9.0 to $cached"
        mkdir -p "$(dirname "$cached")"
        curl -fsSL -o "$cached" \
            https://github.com/asciinema/agg/releases/download/v1.9.0/agg-x86_64-unknown-linux-musl
        chmod +x "$cached"
    fi
    echo "$cached"
}

json() { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }

cleanup() {
    herdr server stop >/dev/null 2>&1 || true
    if [[ -n "${DAEMON:-}" ]]; then
        kill "$DAEMON" 2>/dev/null || true
        wait "$DAEMON" 2>/dev/null || true
    fi
    herdr session delete "$SESSION" >/dev/null 2>&1 || true
    if [[ -n "${DEMO_KEEP:-}" ]]; then say "kept $WORK"; else rm -rf "$WORK"; fi
}
trap cleanup EXIT

AGG=$(agg_binary)
say "building baton-detect"
cargo build --quiet --locked -p baton-detect --manifest-path "$REPO/Cargo.toml"

# The project the task works on.
mkdir -p "$WORK/calc" "$WORK/bin" "$WORK/agents"
cat > "$WORK/calc/calc.py" <<'EOF'
"""A tiny calculator."""


def add(a: float, b: float) -> float:
    return a + b
EOF
git -C "$WORK/calc" init -q -b main
git -C "$WORK/calc" -c user.name=demo -c user.email=demo@example.invalid add . >/dev/null
git -C "$WORK/calc" -c user.name=demo -c user.email=demo@example.invalid commit -qm start

# baton from this checkout, and the fake agents under the real agents' names.
cat > "$WORK/bin/baton" <<EOF
#!/usr/bin/env bash
exec uv run --quiet --project "$REPO" baton "\$@"
EOF
chmod +x "$WORK/bin/baton"
for agent in claude codex; do
    uv run --quiet --project "$REPO" "$REPO/tools/fake-agent/fake_agent.py" \
        --make-launcher "$WORK/agents" "$agent" >/dev/null
done
# A plain prompt: no user or host name on screen.
echo "PS1='\$ '" > "$WORK/demo.bashrc"
cat > "$WORK/bin/demo-shell" <<EOF
#!/usr/bin/env bash
exec bash --noprofile --rcfile "$WORK/demo.bashrc" -i
EOF
chmod +x "$WORK/bin/demo-shell"

cat > "$WORK/baton.toml" <<EOF
[herdr]
session = "$SESSION"
[database]
path = "$WORK/baton.db"
[logging]
level = "warning"
[scheduler]
agents = ["claude", "codex"]
timezone = "UTC"
poll_interval_seconds = 0.5
[scheduler.launch_commands]
claude = "env TZ=UTC $WORK/agents/claude $REPO/tools/fake-agent/scripts/claude-limit.toml"
codex = "env TZ=UTC $WORK/agents/codex $REPO/tools/fake-agent/scripts/codex-finish.toml"
[usage]
enabled = false
[detector]
binary = "$REPO/target/debug/baton-detect"
[policy]
enabled = false
EOF
# No [telegram] chat: the demo sends nothing anywhere.
export BATON_CONFIG="$WORK/baton.toml"
export PATH="$WORK/bin:$PATH"
export SHELL="$WORK/bin/demo-shell"
export TERM=xterm-256color

say "starting a throwaway herdr session"
herdr --session "$SESSION" server >/dev/null 2>&1 &
for _ in $(seq 50); do herdr workspace list >/dev/null 2>&1 && break; sleep 0.2; done

shell=$(herdr workspace create --label demo --cwd "$WORK/calc" --focus | json 'd["result"]["root_pane"]["pane_id"]')
board=$(herdr pane split "$shell" --direction right --ratio 0.6 | json 'd["result"]["pane"]["pane_id"]')
herdr pane send-text "$board" 'while :; do s=$(baton status); clear; printf "# baton status, every second\n\n%s\n" "$s"; sleep 1; done' >/dev/null
herdr pane send-keys "$board" Enter >/dev/null
herdr pane send-text "$shell" "clear" >/dev/null
herdr pane send-keys "$shell" Enter >/dev/null

say "starting batond"
baton daemon --idle 30 >"$WORK/daemon.log" 2>&1 &
DAEMON=$!

type_in() {  # type a command into a pane, a few characters at a time, then Enter
    local pane=$1 text=$2 i
    for ((i = 0; i < ${#text}; i += 4)); do
        herdr pane send-text "$pane" "${text:i:4}" >/dev/null
        sleep 0.04
    done
    sleep 0.4
    herdr pane send-keys "$pane" Enter >/dev/null
}

tab_of() {  # the tab of the newest pane running $1, once there is one
    local agent=$1 tab=""
    for _ in $(seq 100); do
        tab=$(herdr pane list | json "next((p['tab_id'] for p in reversed(d['result']['panes']) if p.get('agent') == '$agent'), '')")
        [[ -n "$tab" ]] && break
        sleep 0.2
    done
    echo "$tab"
}

show() {  # bring the tab where $1 runs to the front
    local tab
    tab=$(tab_of "$1")
    say "showing $1 in tab ${tab:-(none)}"
    [[ -n "$tab" ]] && herdr tab focus "$tab" >/dev/null
}

hand_over() {  # show the new agent's tab once the hand-off made one
    local old new
    old=$(tab_of claude)
    new=$(tab_of codex)
    say "claude's tab $old is done; codex works in $new"
    # An attached client keeps its own tab within a workspace and does not follow
    # `tab focus`; closing the tab of the stopped attempt makes it show the new one.
    [[ -n "$new" ]] && herdr tab close "$old" >/dev/null
}

workspace() {  # the id of the workspace labelled $1
    herdr workspace list | json "next(w['workspace_id'] for w in d['result']['workspaces'] if w['label'] == '$1')"
}

drive() {
    sleep 2
    type_in "$shell" 'baton task add "power function" -i "Add power(a, b) to calc.py with a test."'
    sleep 3
    show claude   # Claude starts, then hits its limit
    sleep 6
    hand_over     # baton hands the task to Codex, which finishes it
    sleep 8
    herdr workspace focus "$(workspace demo)" >/dev/null
    sleep 1
    type_in "$shell" "baton status"
    sleep 2
    type_in "$shell" "baton budget"
    sleep 4
    herdr server stop >/dev/null 2>&1   # ends the client, and with it the recording
}

say "recording"
drive &
DRIVER=$!
uvx --quiet --from asciinema asciinema rec --quiet --overwrite --cols "$COLS" --rows "$ROWS" \
    -c "herdr --session $SESSION" "$WORK/demo.cast" </dev/null
wait "$DRIVER"
# End on the last frame of work, not on the client's farewell as the server stops.
python3 "$REPO/tools/demo/trim_cast.py" "$WORK/demo.cast"

say "rendering $OUT"
mkdir -p "$(dirname "$OUT")"
"$AGG" --quiet --font-size 14 --idle-time-limit 2 --last-frame-duration 4 \
    --font-family "DejaVu Sans Mono,Noto Sans Mono,Liberation Mono" "$WORK/demo.cast" "$OUT"
baton status | grep -q completed || say "warning: the task did not complete"
du -h "$OUT" >&2
