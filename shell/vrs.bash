# vrs shell integration for bash (4.4+).
#
# Records the command line you run in each tmux pane so vrs can offer it
# again after a restore.
#
#   source /path/to/vrs/shell/vrs.bash     # in ~/.bashrc
#
# Uses bash-preexec when it is loaded, otherwise PS0.

[[ -n ${TMUX-} && -n ${TMUX_PANE-} ]] || return 0

__vrs_init() {
  local base=${XDG_RUNTIME_DIR:+$XDG_RUNTIME_DIR/vrs}
  base=${base:-/tmp/vrs-$UID}
  local rest=${TMUX%,*}
  local dir=$base/${rest##*,}
  [[ -d $dir ]] || command mkdir -p -m 700 -- "$dir" 2>/dev/null || return 1
  __vrs_file=$dir/pane-${TMUX_PANE#%}
}

if __vrs_init; then
  if [[ -n ${bash_preexec_imported-} || -n ${__bp_imported-} ]]; then
    __vrs_preexec() { printf '%s' "$1" >| "$__vrs_file" 2>/dev/null; return 0; }
    preexec_functions+=(__vrs_preexec)
  else
    # PS0 is expanded right before each command line runs; the history entry
    # is written by a builtin in the substitution's subshell.
    PS0="${PS0-}"'$(HISTTIMEFORMAT= builtin history 1 >| "$__vrs_file.hist" 2>/dev/null)'
  fi
fi
unset -f __vrs_init
