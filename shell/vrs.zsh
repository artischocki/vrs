# vrs shell integration for zsh.
#
# Records the exact command line you run in each tmux pane (aliases, pipes,
# quoting and all), so vrs can offer it again after a restore. Without it vrs
# rebuilds commands from the running processes, which works but is less exact.
#
#   source /path/to/vrs/shell/vrs.zsh      # in ~/.zshrc
#
# Cost per command: one builtin write to a file on tmpfs, no fork.

[[ -n ${TMUX-} && -n ${TMUX_PANE-} ]] || return 0

() {
  local base=${XDG_RUNTIME_DIR:+$XDG_RUNTIME_DIR/vrs}
  : ${base:=/tmp/vrs-$UID}
  local server_pid=${${TMUX%,*}##*,}
  local dir=$base/$server_pid
  [[ -d $dir ]] || command mkdir -p -m 700 -- $dir 2>/dev/null || return 0
  typeset -g _vrs_cmd_file=$dir/pane-${TMUX_PANE#%}
}

_vrs_preexec() {
  [[ -n ${_vrs_cmd_file-} ]] && print -rn -- "$1" >| $_vrs_cmd_file 2>/dev/null
  return 0
}

autoload -Uz add-zsh-hook
add-zsh-hook preexec _vrs_preexec
