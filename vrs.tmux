#!/usr/bin/env bash
# vrs - save and restore tmux sessions, panes, commands and Claude Code sessions.
# Loaded by tpm, or directly with: run-shell /path/to/vrs/vrs.tmux

CURRENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v python3 >/dev/null 2>&1; then
    tmux display-message "vrs: python3 is required"
    exit 0
fi

python3 "$CURRENT_DIR/scripts/vrs.py" init
