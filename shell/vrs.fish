# vrs shell integration for fish.
#
# Records the command line you run in each tmux pane so vrs can offer it
# again after a restore.
#
#   source /path/to/vrs/shell/vrs.fish     # in ~/.config/fish/config.fish

if set -q TMUX; and set -q TMUX_PANE
    set -l base /tmp/vrs-(id -u)
    if set -q XDG_RUNTIME_DIR
        set base $XDG_RUNTIME_DIR/vrs
    end
    set -l parts (string split , -- $TMUX)
    set -l dir $base/$parts[-2]
    if test -d $dir; or mkdir -p -m 700 -- $dir 2>/dev/null
        set -g __vrs_file $dir/pane-(string replace % '' -- $TMUX_PANE)
        function __vrs_preexec --on-event fish_preexec
            printf '%s' $argv[1] >$__vrs_file 2>/dev/null
        end
    end
end
