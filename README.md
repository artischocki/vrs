# vrs

Save and restore tmux, including what was running in each pane.

vrs saves:

- **sessions, windows and panes**, with window names, layouts and zoom, plus which window and pane were active and which were last
- the **working directory** of every pane
- the **command running in every pane**
- the **Claude Code session** open in every pane, matched by its exact session ID

When it restores:

- **nvim** starts again right away
- **Claude Code** sessions resume right away: `claude --resume <session-id>` runs in the session's directory, with the flags it was started with (`--model`, `--dangerously-skip-permissions`, `--add-dir`, ...)
- **every other command** waits. The pane opens in the right directory, and the first time you switch to it vrs asks in the status line:

  ```
  vrs: rerun `npm run dev` here? (y/n)
  ```

  `y` types the command into the pane's shell and runs it, so it lands in your shell history and Ctrl-C drops you back to the prompt. Any other key skips it.

## Install

With [tpm](https://github.com/tmux-plugins/tpm), add this to `tmux.conf`, then press `prefix + I`:

```tmux
set -g @plugin 'artischocki/vrs'
```

Without tpm, clone the repo and add `run-shell /path/to/vrs/vrs.tmux` to `tmux.conf`.

Requirements: tmux ≥ 3.2, python3 (standard library only), Linux (vrs reads `/proc`).

### Shell integration (recommended)

vrs always works out which command runs in a pane by reading `/proc`. Source the integration for your shell and it uses the exact line you typed instead, with aliases, pipes, `&&` chains, quoting and env-var prefixes intact:

```sh
# ~/.zshrc
source ~/.config/tmux/plugins/vrs/shell/vrs.zsh
# ~/.bashrc (bash 4.4+; uses bash-preexec if loaded)
source ~/.config/tmux/plugins/vrs/shell/vrs.bash
# ~/.config/fish/config.fish
source ~/.config/tmux/plugins/vrs/shell/vrs.fish
```

On each command, the hook writes the command line to a small file in `$XDG_RUNTIME_DIR`. In zsh that write is a builtin and doesn't fork a process.

## Usage

| key            | action                         |
| -------------- | ------------------------------ |
| `prefix + C-s` | save now                       |
| `prefix + C-r` | restore the latest snapshot    |

Saving and restoring also happen on their own:

- **autosave** every 5 minutes, and whenever a client detaches. A snapshot is only written if something changed.
- **restore on start**: when the tmux server starts, vrs restores the latest snapshot. The empty session tmux started with is closed, and your client moves to the session you were in before.

Restoring never touches sessions that already exist. Sessions with the same name are skipped, so pressing `prefix + C-r` twice does no harm.

From the command line:

```sh
~/.config/tmux/plugins/vrs/scripts/vrs.py list              # snapshots, newest first
~/.config/tmux/plugins/vrs/scripts/vrs.py restore FILE      # restore an older one
```

Snapshots are plain JSON in `~/.local/share/vrs/<socket>/snapshots/`. `last` points to the newest snapshot, and the 100 most recent are kept.

## Options

```tmux
set -g @vrs-save-key 'C-s'            # '' to unbind
set -g @vrs-restore-key 'C-r'         # '' to unbind
set -g @vrs-interval '5'              # autosave every N minutes, 0 = off
set -g @vrs-save-on-detach 'on'
set -g @vrs-restore-on-start 'on'
set -g @vrs-autorun 'nvim'            # programs restored without asking, e.g. 'nvim htop'
set -g @vrs-claude 'on'               # resume Claude Code sessions
set -g @vrs-claude-cmd 'claude'
set -g @vrs-ask 'on'                  # offer to rerun other commands
set -g @vrs-ask-idle 'off'            # also offer the last command of idle shells (needs shell integration)
set -g @vrs-confirm-enter 'off'       # Enter also answers "yes" in the prompt
set -g @vrs-keep '100'                # snapshots to keep
set -g @vrs-dir '~/.local/share/vrs'
```

## How it works

- **Layout:** comes from `list-panes -a`. Restoring recreates each window at its saved size, splits it to the saved pane count and applies the saved layout string.
- **Running command:** for each pane, vrs finds the terminal's foreground process group: `tpgid` in `/proc/<shell>/stat`. If the shell itself is in the foreground, the pane is idle. Otherwise the command line comes from the shell integration's record, or is rebuilt from `/proc/<pid>/cmdline` (pipelines included).
- **Claude Code:** every running session registers itself in `~/.claude/sessions/<pid>.json` with its session ID and directory. vrs matches that against the processes in the pane, and checks the process start time so a reused PID can't fool it. On restore it runs `claude --resume <id>` if the transcript exists. If the session never got a message, it starts a fresh `claude` with the same flags. `CLAUDE_CONFIG_DIR` is respected.
- **Asking:** pending commands live in a per-server runtime file. tmux hooks (`client-attached`, `client-session-changed`, `session-window-changed`, `window-pane-changed`) check one global flag. Only while commands are pending do they call vrs, which shows `confirm-before` to the client looking at the pane.

## Tests

```sh
python3 -m unittest discover tests     # unit tests
tests/integration.sh                   # end to end, on private tmux sockets
```

The integration test builds a server with splits, a custom layout, nvim, pipelines and two (fake) Claude sessions. It saves, kills the server, starts a new one with a real client attached and checks everything came back, prompts included. It never touches your own tmux server.
