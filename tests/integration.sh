#!/usr/bin/env bash
# End-to-end test: build a tmux server, save it, kill it, start a fresh one
# with a real client attached and check that vrs brought everything back.
#
# Runs on private sockets with its own config, runtime and Claude dirs, so it
# never touches your tmux server. Needs tmux, python3, zsh and nvim.
#
#   tests/integration.sh

set -u

REPO=$(cd "$(dirname "$0")/.." && pwd)
WORK=$(mktemp -d "${TMPDIR:-/tmp}/vrs-test.XXXXXX")
SOCK=vrs-test-$$
OUTER=vrs-test-outer-$$
VRS="python3 $REPO/scripts/vrs.py"

export VRS_TEST_DIR=$WORK
export CLAUDE_CONFIG_DIR=$WORK/claude
export XDG_RUNTIME_DIR=$WORK/run
export ZDOTDIR=$WORK/zdot
export PATH=$REPO/tests/bin:$PATH
unset TMUX TMUX_PANE

mkdir -p "$XDG_RUNTIME_DIR" "$ZDOTDIR" "$WORK/a" "$WORK/b" "$WORK/c" "$WORK/d d"
chmod 700 "$XDG_RUNTIME_DIR"
cat > "$ZDOTDIR/.zshrc" <<EOF
PS1='%~ %# '
source '$REPO/shell/vrs.zsh'
EOF
cat > "$WORK/tmux.conf" <<EOF
set -g base-index 1
set -g pane-base-index 1
set -g default-shell $(command -v zsh)
set -g @vrs-dir '$WORK/data'
set -g @vrs-interval 0
run-shell '$REPO/vrs.tmux'
EOF

t() { tmux -L "$SOCK" -f "$WORK/tmux.conf" "$@"; }
o() { tmux -L "$OUTER" -f /dev/null "$@"; }

cleanup() {
    t kill-server 2>/dev/null
    o kill-server 2>/dev/null
    pkill -f "$REPO/tests/fake_claude.py" 2>/dev/null
    rm -rf "$WORK"
}
trap cleanup EXIT

PASS=0
FAIL=0
ok() { PASS=$((PASS + 1)); echo "ok   $1"; }
fail() { FAIL=$((FAIL + 1)); echo "FAIL $1"; }
check() { local desc=$1; shift; if "$@"; then ok "$desc"; else fail "$desc"; fi; }
eq() {
    [[ "$1" == "$2" ]] && return 0
    printf '     expected: %s\n     got:      %s\n' "$2" "$1"
    return 1
}
wait_for() { # wait_for 'condition' [tenths of a second]
    local i
    for ((i = 0; i < ${2:-50}; i++)); do
        eval "$1" && return 0
        sleep 0.1
    done
    return 1
}
cmd_of() { t display -p -t "$1" '#{pane_current_command}'; }
geometry() { t list-panes -t "$1" -F '#{pane_index}:#{pane_left},#{pane_top},#{pane_width}x#{pane_height}' | tr '\n' ' '; }
status_line() { o capture-pane -p -t outer | tail -1; }

# --- 1. build some state ---------------------------------------------------

o new-session -d -s outer -x 160 -y 42 "env -u TMUX tmux -L $SOCK -f $WORK/tmux.conf new-session -s misc -c '$WORK/b'"
wait_for 't has-session -t misc 2>/dev/null' || { echo "inner server did not start"; exit 1; }
CLIENT=$(t list-clients -F '#{client_name}')

t split-window -t misc:1 -c "$WORK/c"
t split-window -t misc:1 -c "$WORK/a"
t select-layout -t misc:1 main-vertical
t send-keys -t misc:1.3 'sleep 1002 | cat' Enter

t new-session -d -s work -c "$WORK/a"
t split-window -h -t work:1 -c "$WORK/b"
t send-keys -t work:1.2 'sleep 1001 | cat' Enter
t new-window -t work:2 -n editor -c "$WORK/c"
t send-keys -t work:2.1 'nvim -u NONE -n notes.txt' Enter
t new-window -t work:3 -c "$WORK/d d"
t send-keys -t work:3.1 "claude --model opus --dangerously-skip-permissions 'fix the bug'" Enter
t split-window -v -t work:3 -c "$WORK/a"
t send-keys -t work:3.2 'claude --no-transcript' Enter
t resize-pane -Z -t work:3.1
t link-window -d -s misc:1 -t work:5
t new-session -d -s view -t work
t select-window -t view:2
t select-window -t work:3
t select-window -t work:1
t select-pane -t work:1.1
t switch-client -c "$CLIENT" -t work

wait_for '[[ $(cmd_of work:1.2) == sleep && $(cmd_of misc:1.3) == sleep ]]' || fail "commands did not start"
wait_for '[[ $(cmd_of work:2.1) == nvim ]]' || fail "nvim did not start"
wait_for '[[ $(ls "$CLAUDE_CONFIG_DIR/sessions" 2>/dev/null | wc -l) -ge 2 ]]' || fail "fake claude did not start"
sleep 0.5

# without the shell integration's record vrs falls back to /proc
SERVER_PID=$(t display -p '#{pid}')
rm -f "$XDG_RUNTIME_DIR/vrs/$SERVER_PID/pane-$(t display -p -t misc:1.3 '#{pane_id}' | tr -d %)"

GEO_WORK1=$(geometry work:1)
GEO_MISC1=$(geometry misc:1)
CLAUDE_PID=$(pgrep -f "fake_claude.py --model opus" | head -1)
CLAUDE_SID=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['sessionId'])" \
    "$CLAUDE_CONFIG_DIR/sessions/$CLAUDE_PID.json")

t run-shell "$VRS save"
SNAP="$WORK/data/vrs-test-$$/last"
check "snapshot written" test -f "$SNAP"

python3 - "$SNAP" "$WORK" "$CLAUDE_SID" <<'EOF'
import json, sys
snap, work, sid = json.load(open(sys.argv[1])), sys.argv[2], sys.argv[3]
fails = []
def expect(desc, got, want):
    if got != want:
        fails.append("%s: expected %r, got %r" % (desc, want, got))
s = {x["name"]: x for x in snap["sessions"]}
expect("sessions", sorted(s), ["misc", "view", "work"])
expect("grouped session", (s["view"].get("group"), s["view"].get("active_window"), s["view"]["windows"]),
       ("work", 2, []))
expect("active session", snap["active_session"], "work")
expect("last session", snap["last_session"], "misc")
w = {x["index"]: x for x in s["work"]["windows"]}
expect("work windows", sorted(w), [1, 2, 3, 5])
expect("linked window", w[5].get("link"), {"session": "misc", "index": 1})
expect("zoomed", w[3]["zoomed"], True)
expect("editor name", (w[2]["name"], w[2]["auto_rename"]), ("editor", False))
expect("window 1 auto-named", w[1]["auto_rename"], True)
p = w[1]["panes"]
expect("work:1 cwds", [x["cwd"] for x in p], [work + "/a", work + "/b"])
expect("work:1.1 idle", "process" in p[0], False)
expect("typed pipeline", (p[1]["process"]["command"], p[1]["process"]["source"]),
       ("sleep 1001 | cat", "shell"))
nv = w[2]["panes"][0]["process"]
expect("nvim", (nv["program"], nv["command"]), ("nvim", "nvim -u NONE -n notes.txt"))
c1, c2 = (x["claude"] for x in w[3]["panes"])
expect("claude session", c1["session_id"], sid)
expect("claude cwd", c1["cwd"], work + "/d d")
expect("claude flags", c1["args"], ["--model", "opus", "--dangerously-skip-permissions"])
expect("claude 2 flags", c2["args"], [])
m = s["misc"]["windows"][0]["panes"]
expect("proc pipeline", (m[2]["process"]["command"], m[2]["process"]["source"]),
       ("sleep 1002 | cat", "proc"))
for f in fails:
    print("     " + f)
sys.exit(1 if fails else 0)
EOF
check "snapshot contents" test $? -eq 0

# --- 2. kill everything, start over ------------------------------------------

t kill-server
wait_for '! o has-session 2>/dev/null' 30
o kill-server 2>/dev/null
pkill -f "$REPO/tests/fake_claude.py" 2>/dev/null
sleep 0.5
: > "$WORK/claude-invocations"

# a plain `tmux` start: the plugin restores into it automatically
o new-session -d -s outer -x 160 -y 42 "env -u TMUX tmux -L $SOCK -f $WORK/tmux.conf"
wait_for 't has-session -t work 2>/dev/null && ! t has-session -t =0 2>/dev/null' 100
check "restored on start, throwaway session gone" \
    eq "$(t list-sessions -F '#{session_name}' | sort | tr '\n' ' ')" "misc view work "
CLIENT=$(t list-clients -F '#{client_name}')
check "client moved to the active session" eq "$(t display -p -c "$CLIENT" '#{client_session}')" "work"
check "previous session kept for switch-client -l" eq "$(t display -p -c "$CLIENT" '#{client_last_session}')" "misc"
check "windows" eq "$(t list-windows -t work -F '#{window_index}' | tr '\n' ' ')" "1 2 3 5 "
check "linked window" eq "$(t display -p -t work:5 '#{window_id}')" "$(t display -p -t misc:1 '#{window_id}')"
check "grouped session" eq "$(t display -p -t view '#{session_grouped} #{session_windows} #{window_index}')" "1 4 2"
check "zoom" eq "$(t display -p -t work:3 '#{window_zoomed_flag}')" "1"
check "window name" eq "$(t display -p -t work:2 '#{window_name}')" "editor"
check "active window" eq "$(t display -p -t work '#{window_index}')" "1"
check "last window" eq "$(t list-windows -t work -F '#{window_last_flag}#{window_index}' | grep '^1')" "13"
check "work:1 layout" eq "$(geometry work:1)" "$GEO_WORK1"
check "misc:1 layout" eq "$(geometry misc:1)" "$GEO_MISC1"
check "pane cwds" eq "$(t list-panes -t work:1 -F '#{pane_current_path}' | tr '\n' ' ')" "$WORK/a $WORK/b "
check "claude cwd" eq "$(t display -p -t work:3.1 '#{pane_current_path}')" "$WORK/d d"

wait_for '[[ $(cmd_of work:2.1) == nvim ]]' 60
check "nvim restarted without asking" eq "$(cmd_of work:2.1)" "nvim"
wait_for '[[ $(grep -c . "$WORK/claude-invocations") -ge 2 ]]' 60
check "claude resumed with its flags" \
    grep -qxF -- "--resume $CLAUDE_SID --model opus --dangerously-skip-permissions" "$WORK/claude-invocations"
check "claude without transcript started fresh" grep -qx -- "" "$WORK/claude-invocations"

check "other commands wait" eq "$(cmd_of work:1.2) $(cmd_of misc:1.3)" "zsh zsh"
check "no prompt on an idle pane" bash -c "! tmux -L $OUTER capture-pane -p -t outer | grep -q 'vrs: rerun'"

# switching to a pane with a saved command asks first
t select-pane -t work:1.2
wait_for '[[ $(status_line) == *"vrs: rerun"* ]]' 30
check "asks when switching to the pane" eq "$(status_line | sed 's/ *$//')" 'vrs: rerun `sleep 1001 | cat` here? (y/n)'
o send-keys -t outer y
wait_for '[[ $(cmd_of work:1.2) == sleep ]]' 30
check "y reruns the command" eq "$(cmd_of work:1.2)" "sleep"
sleep 0.3
check "asks only once" bash -c "! tmux -L $OUTER capture-pane -p -t outer | grep -q 'vrs: rerun'"

# switching sessions counts too; n leaves the pane alone
t switch-client -c "$CLIENT" -t misc
wait_for '[[ $(status_line) == *"vrs: rerun"* ]]' 30
check "asks after switching session" eq "$(status_line | sed 's/ *$//')" 'vrs: rerun `sleep 1002 | cat` here? (y/n)'
o send-keys -t outer n
sleep 0.5
check "n skips the command" eq "$(cmd_of misc:1.3)" "zsh"
check "nothing pending afterwards" eq "$(t show -gqv @vrs-pending-any)" ""

# restoring again does not duplicate sessions
t run-shell "$VRS restore"
check "restore skips running sessions" eq "$(t list-sessions | grep -c .)" "3"

# saving the restored state: the claude pane points at the same session
t run-shell "$VRS save"
check "resumed claude session saved again" grep -q "$CLAUDE_SID" "$SNAP"

# --- 3. autosave ------------------------------------------------------------

t set -g @vrs-interval 0.03  # ~2 seconds
t run-shell "python3 $REPO/scripts/vrs.py init"
BEFORE=$(readlink "$SNAP")
t new-window -t work -c "$WORK/c"
wait_for '[[ $(readlink "$SNAP") != "$BEFORE" ]]' 100
check "autosave picks up changes" test "$(readlink "$SNAP")" != "$BEFORE"
check "one autosave daemon" eq "$(pgrep -fc "$REPO/scripts/vrs.py daemon")" "1"
t set -g @vrs-interval 0

echo
echo "$PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
