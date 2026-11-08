#!/usr/bin/env python3
"""vrs - save and restore tmux.

Saves every session, window and pane (with its layout and working directory),
the command running in each pane and the exact Claude Code session open in
each pane. Restores all of it: nvim and Claude sessions come back right away,
and for any other command vrs asks whether to rerun it the first time you
switch to that pane.

Usage:
    vrs.py save [--auto]       write a snapshot
    vrs.py restore [FILE]      restore the latest (or the given) snapshot
    vrs.py list                list snapshots, newest first
    vrs.py init                set up key bindings, hooks and autosave (vrs.tmux)
    vrs.py focus               hook handler: offer pending commands
    vrs.py run PANE            hook handler: run the offered command in PANE
    vrs.py daemon              autosave loop (started by init)
"""

import argparse
import fcntl
import glob
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import traceback
from contextlib import contextmanager
from datetime import datetime

SNAPSHOT_VERSION = 1
SEP = "␞"  # field separator for tmux format output; never seen in real names
HOOK_INDEX = 870  # fixed slot in the hook arrays, so reloading the config is idempotent
SCRIPT = os.path.realpath(__file__)

SHELLS = {
    "sh", "bash", "zsh", "fish", "dash", "ksh", "mksh", "oksh", "loksh", "pdksh",
    "tcsh", "csh", "yash", "nu", "xonsh", "elvish", "ash", "busybox",
}
# a typed command that resumes or inspects jobs says nothing about what is running
JOB_CONTROL_RE = re.compile(r"^\s*(fg|bg|jobs|wait|disown)?\s*(%\S*)?\s*$|^\s*(fg|bg)\b")

DEFAULTS = {
    "@vrs-dir": "",  # default: $XDG_DATA_HOME/vrs (~/.local/share/vrs)
    "@vrs-save-key": "C-s",
    "@vrs-restore-key": "C-r",
    "@vrs-interval": "5",  # autosave interval in minutes, 0 disables
    "@vrs-keep": "100",  # snapshots to keep
    "@vrs-restore-on-start": "on",
    "@vrs-save-on-detach": "on",
    "@vrs-autorun": "nvim",  # programs restored without asking
    "@vrs-claude": "on",  # resume Claude Code sessions
    "@vrs-claude-cmd": "claude",
    "@vrs-ask": "on",  # offer to rerun other commands
    "@vrs-ask-idle": "off",  # also offer the last command of idle shells
    "@vrs-confirm-enter": "off",  # Enter also confirms the prompt
}


class TmuxError(Exception):
    pass


# --------------------------------------------------------------------------
# tmux helpers
# --------------------------------------------------------------------------

def tmux(*args, check=True):
    proc = subprocess.run(
        ["tmux"] + [str(a) for a in args],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if check and proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()
        raise TmuxError("tmux %s: %s" % (args[0], err))
    return proc.stdout.decode("utf-8", "surrogateescape")


def tmux_ok(*args):
    try:
        tmux(*args)
        return True
    except TmuxError:
        return False


def tmux_rows(*args):
    """Run a tmux list command whose format separates fields with SEP."""
    out = tmux(*args)
    return [line.split(SEP) for line in out.splitlines() if line]


def fmt(*fields):
    return SEP.join("#{%s}" % f for f in fields)


def option(name):
    out = tmux("show-option", "-gqv", name, check=False).rstrip("\n")
    # show-option escapes '$' in values
    out = out.replace("\\$", "$")
    return out if out != "" else DEFAULTS.get(name, "")


def option_on(name):
    return option(name).lower() in ("on", "yes", "true", "1")


def option_int(name):
    try:
        return int(option(name))
    except ValueError:
        return int(DEFAULTS[name])


def option_float(name):
    try:
        return float(option(name))
    except ValueError:
        return float(DEFAULTS[name])


def tmux_quote(s):
    """Quote a string as one argument for tmux's command parser."""
    return '"%s"' % s.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$")


def shell_line(*args):
    """Shell command line that calls this script, escaped for run-shell
    (which expands formats in its argument)."""
    sh = " ".join(shlex.quote(a) for a in (sys.executable, SCRIPT) + args)
    return sh.replace("#", "##")


def run_shell_cmd(*args):
    """A run-shell command (as tmux command text) that calls this script."""
    return "run-shell -b " + tmux_quote(shell_line(*args))


def message(text):
    if sys.stdout.isatty():
        print(text)
    tmux("display-message", text.replace("#", "##"), check=False)


def server_info():
    """(socket_path, server_pid) of the tmux server we talk to."""
    env = os.environ.get("TMUX", "")
    parts = env.rsplit(",", 2)
    if len(parts) == 3 and parts[1].isdigit():
        return parts[0], int(parts[1])
    out = tmux("display-message", "-p", "#{socket_path}" + SEP + "#{pid}").strip()
    path, pid = out.split(SEP)
    return path, int(pid)


# --------------------------------------------------------------------------
# paths and files
# --------------------------------------------------------------------------

def data_dir():
    base = option("@vrs-dir")
    if base:
        base = os.path.expanduser(os.path.expandvars(base))
    else:
        xdg = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
        base = os.path.join(xdg, "vrs")
    socket_path, _ = server_info()
    path = os.path.join(base, os.path.basename(socket_path) or "default")
    os.makedirs(path, exist_ok=True)
    return path


def runtime_base():
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return os.path.join(xdg, "vrs")
    return "/tmp/vrs-%d" % os.getuid()


def runtime_dir():
    """Per tmux server scratch dir; the shell integration writes here too."""
    _, pid = server_info()
    path = os.path.join(runtime_base(), str(pid))
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


@contextmanager
def locked(name, blocking=True):
    path = os.path.join(runtime_dir(), name)
    with open(path, "a") as fh:
        flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(fh, flags)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def write_json_atomic(path, data):
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=1, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


def read_json(path, default=None):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def log_error(where):
    try:
        with open(os.path.join(data_dir(), "vrs.log"), "a") as fh:
            fh.write("%s %s\n%s\n" % (datetime.now().isoformat(timespec="seconds"),
                                      where, traceback.format_exc()))
    except Exception:
        pass


# --------------------------------------------------------------------------
# processes (/proc)
# --------------------------------------------------------------------------

CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
_BOOT_TIME = None


def boot_time():
    global _BOOT_TIME
    if _BOOT_TIME is None:
        _BOOT_TIME = 0.0
        try:
            with open("/proc/stat") as fh:
                for line in fh:
                    if line.startswith("btime "):
                        _BOOT_TIME = float(line.split()[1])
                        break
        except OSError:
            pass
    return _BOOT_TIME


class Proc:
    __slots__ = ("pid", "ppid", "pgrp", "tpgid", "comm", "start")

    def __init__(self, pid, ppid, pgrp, tpgid, comm, start):
        self.pid, self.ppid, self.pgrp, self.tpgid = pid, ppid, pgrp, tpgid
        self.comm, self.start = comm, start

    @property
    def started_at(self):
        return boot_time() + self.start / CLK_TCK


def read_proc(pid):
    try:
        with open("/proc/%d/stat" % pid, "rb") as fh:
            data = fh.read().decode("utf-8", "replace")
    except OSError:
        return None
    lp, rp = data.find("("), data.rfind(")")
    fields = data[rp + 2:].split()
    try:
        return Proc(pid, int(fields[1]), int(fields[2]), int(fields[5]),
                    data[lp + 1:rp], int(fields[19]))
    except (IndexError, ValueError):
        return None


def scan_procs():
    procs = {}
    for name in os.listdir("/proc"):
        if name.isdigit():
            p = read_proc(int(name))
            if p:
                procs[p.pid] = p
    return procs


def proc_argv(pid):
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as fh:
            data = fh.read()
    except OSError:
        return []
    parts = data.split(b"\0")
    while parts and parts[-1] == b"":
        parts.pop()
    return [p.decode("utf-8", "surrogateescape") for p in parts]


def proc_cwd(pid):
    try:
        return os.readlink("/proc/%d/cwd" % pid)
    except OSError:
        return None


def proc_env(pid, key):
    try:
        with open("/proc/%d/environ" % pid, "rb") as fh:
            data = fh.read()
    except OSError:
        return None
    prefix = key.encode() + b"="
    for item in data.split(b"\0"):
        if item.startswith(prefix):
            return item[len(prefix):].decode("utf-8", "surrogateescape")
    return None


def program_name(argv, comm=""):
    if argv:
        first = argv[0]
        if len(argv) == 1 and " " in first and not os.path.exists(first):
            first = first.split()[0]  # process rewrote its title
        name = os.path.basename(first).lstrip("-")
        if name:
            return name
    return comm.lstrip("-")


def is_descendant(pid, ancestor, procs):
    seen = 0
    while pid and pid in procs and seen < 64:
        if pid == ancestor:
            return True
        pid = procs[pid].ppid
        seen += 1
    return False


def argv_to_command(argv):
    if not argv:
        return ""
    if len(argv) == 1 and " " in argv[0] and not os.path.exists(argv[0]):
        return argv[0].strip()  # e.g. node processes that set process.title
    first = argv[0].lstrip("-")
    if "/" in first:
        base = os.path.basename(first)
        if shutil.which(base):
            first = base  # /snap/nvim/123/usr/bin/nvim -> nvim
    return shlex.join([first] + argv[1:])


def foreground(pane_pid, procs):
    """Return (shell_is_shell, foreground processes) of a pane.

    The foreground processes are the members of the terminal's foreground
    process group (the leader first), or [] when the shell itself is idle.
    """
    shell = procs.get(pane_pid)
    if shell is None:
        return True, []
    is_shell = program_name(proc_argv(pane_pid), shell.comm) in SHELLS
    fg = shell.tpgid
    if fg <= 0 or fg == shell.pgrp:
        return is_shell, ([] if is_shell else [shell])
    members = [p for p in procs.values()
               if p.pgrp == fg and is_descendant(p.pid, pane_pid, procs)]
    members.sort(key=lambda p: (p.pid != fg, p.start, p.pid))
    if not members:
        return is_shell, ([] if is_shell else [shell])
    return is_shell, members


def proc_command(pane_pid, members):
    """Rebuild a runnable command line from the foreground processes."""
    # a pipeline shows up as several direct children of the shell
    direct = sorted((p for p in members if p.ppid == pane_pid), key=lambda p: p.pid)
    if len(direct) > 1:
        return " | ".join(argv_to_command(proc_argv(p.pid)) for p in direct)
    return argv_to_command(proc_argv(members[0].pid))


# --------------------------------------------------------------------------
# shell integration (exact typed command lines)
# --------------------------------------------------------------------------

HIST_PREFIX_RE = re.compile(r"^\s*\d+\*?\s+")


def typed_command(pane_id):
    """(command, mtime) recorded by the shell integration, or (None, None)."""
    num = pane_id.lstrip("%")
    rt = runtime_dir()
    for name, strip_hist in (("pane-%s" % num, False), ("pane-%s.hist" % num, True)):
        path = os.path.join(rt, name)
        try:
            with open(path, "rb") as fh:
                text = fh.read().decode("utf-8", "surrogateescape")
            mtime = os.stat(path).st_mtime
        except OSError:
            continue
        if strip_hist:
            text = HIST_PREFIX_RE.sub("", text, count=1)
        text = text.rstrip("\n")
        if text.strip():
            return text, mtime
    return None, None


# --------------------------------------------------------------------------
# Claude Code
# --------------------------------------------------------------------------

CLAUDE_KEEP_FLAG = {
    "--dangerously-skip-permissions", "--allow-dangerously-skip-permissions",
    "--chrome", "--no-chrome", "--ide", "--verbose", "--brief", "--bare",
    "--safe-mode", "--restricted", "--strict-mcp-config",
    "--disable-slash-commands", "--ax-screen-reader",
    "--exclude-dynamic-system-prompt-sections",
}
CLAUDE_KEEP_VALUE = {
    "--model", "--permission-mode", "--effort", "--agent", "--agents",
    "--settings", "--fallback-model", "--append-system-prompt",
    "--system-prompt", "--setting-sources", "--autocompact", "--debug-file",
    "--plugin-dir", "--plugin-url", "--system-prompt-snapshot",
    "--remote-control-session-name-prefix",
}
CLAUDE_KEEP_OPTIONAL = {"--remote-control"}
CLAUDE_KEEP_MULTI = {
    "--add-dir", "--allowedTools", "--allowed-tools", "--disallowedTools",
    "--disallowed-tools", "--mcp-config", "--tools", "--betas",
}
CLAUDE_DROP_VALUE = {
    "--session-id", "-n", "--name", "--environment", "--input-format",
    "--output-format", "--json-schema", "--max-budget-usd",
    "--permission-prompts",
}
CLAUDE_DROP_OPTIONAL = {
    "-r", "--resume", "--from-pr", "-w", "--worktree", "--cloud",
    "--teleport", "-d", "--debug", "--prompt-suggestions",
}
CLAUDE_DROP_MULTI = {"--file"}


def claude_keep_args(args):
    """Flags worth passing again when resuming; drops session selection,
    prompts and anything unknown."""
    keep, i = [], 0
    while i < len(args):
        arg = args[i]
        i += 1
        if arg == "--":
            break
        if not arg.startswith("-"):
            continue  # positional (prompt)
        name, has_eq, _ = arg.partition("=")
        if name in CLAUDE_KEEP_FLAG:
            keep.append(arg)
        elif name in CLAUDE_KEEP_VALUE:
            keep.append(arg)
            if not has_eq and i < len(args):
                keep.append(args[i])
                i += 1
        elif name in CLAUDE_KEEP_OPTIONAL:
            keep.append(arg)
            if not has_eq and i < len(args) and not args[i].startswith("-"):
                keep.append(args[i])
                i += 1
        elif name in CLAUDE_KEEP_MULTI:
            keep.append(arg)
            if not has_eq:
                while i < len(args) and not args[i].startswith("-"):
                    keep.append(args[i])
                    i += 1
        elif name in CLAUDE_DROP_VALUE:
            if not has_eq:
                i += 1
        elif name in CLAUDE_DROP_OPTIONAL:
            if not has_eq and i < len(args) and not args[i].startswith("-"):
                i += 1
        elif name in CLAUDE_DROP_MULTI:
            while not has_eq and i < len(args) and not args[i].startswith("-"):
                i += 1
    return keep


def default_claude_dir():
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")


def claude_session_for(proc):
    """The Claude Code session a process runs, from <config>/sessions/<pid>.json."""
    dirs = []
    env_dir = proc_env(proc.pid, "CLAUDE_CONFIG_DIR")
    for d in (env_dir, default_claude_dir(), os.path.expanduser("~/.claude")):
        if d and d not in dirs:
            dirs.append(d)
    for d in dirs:
        info = read_json(os.path.join(d, "sessions", "%d.json" % proc.pid))
        if not isinstance(info, dict) or not info.get("sessionId"):
            continue
        if "procStart" in info and str(info["procStart"]) != str(proc.start):
            continue  # stale file of an older process with the same pid
        return info, (env_dir or None)
    return None, None


def claude_from_argv(argv):
    """Session id named on the command line, if any."""
    for i, arg in enumerate(argv):
        name, has_eq, value = arg.partition("=")
        if name in ("-r", "--resume", "--session-id"):
            if not has_eq and i + 1 < len(argv):
                value = argv[i + 1]
            if re.fullmatch(r"[0-9a-fA-F-]{36}", value or ""):
                return value
    return None


def detect_claude(members):
    for p in members:
        info, env_dir = claude_session_for(p)
        argv = proc_argv(p.pid)
        if info:
            return {
                "session_id": info["sessionId"],
                "cwd": info.get("cwd") or proc_cwd(p.pid),
                "name": info.get("name"),
                "args": claude_keep_args(argv[1:]),
                "config_dir": env_dir,
            }
        if program_name(argv, p.comm) == "claude":
            return {
                "session_id": claude_from_argv(argv),
                "cwd": proc_cwd(p.pid),
                "name": None,
                "args": claude_keep_args(argv[1:]),
                "config_dir": proc_env(p.pid, "CLAUDE_CONFIG_DIR"),
            }
    return None


def claude_transcript_exists(c):
    cfg = c.get("config_dir") or default_claude_dir()
    return bool(glob.glob(os.path.join(glob.escape(cfg), "projects", "*",
                                       glob.escape(c["session_id"]) + ".jsonl")))


def claude_command(c):
    argv = [option("@vrs-claude-cmd") or "claude"]
    if c.get("session_id"):
        if claude_transcript_exists(c):
            argv += ["--resume", c["session_id"]]
        # else: the session never got a message, so start a fresh one
    else:
        argv += ["--continue"]
    argv += c.get("args") or []
    cmd = " ".join([argv[0]] + [shlex.quote(a) for a in argv[1:]])
    cfg = c.get("config_dir")
    if cfg and os.path.normpath(cfg) != os.path.normpath(default_claude_dir()):
        cmd = "CLAUDE_CONFIG_DIR=%s %s" % (shlex.quote(cfg), cmd)
    return cmd


# --------------------------------------------------------------------------
# save
# --------------------------------------------------------------------------

PANE_FIELDS = (
    "session_id", "session_name", "session_last_attached",
    "window_id", "window_index", "window_name", "window_layout",
    "window_active", "window_last_flag", "window_zoomed_flag",
    "automatic-rename", "window_width", "window_height",
    "pane_id", "pane_index", "pane_active", "pane_last", "pane_pid",
    "pane_current_path",
)


def as_int(s, default=0):
    try:
        return int(s)
    except (TypeError, ValueError):
        return default


def capture_pane(pane, procs):
    pane_pid = as_int(pane["pane_pid"])
    is_shell, members = foreground(pane_pid, procs)
    shell_cwd = proc_cwd(pane_pid) if is_shell else None
    rec = {
        "index": as_int(pane["pane_index"]),
        "active": pane["pane_active"] == "1",
        "last": pane["pane_last"] == "1",
        "cwd": shell_cwd or pane["pane_current_path"],
        "path": pane["pane_current_path"],
    }
    typed, typed_at = typed_command(pane["pane_id"])
    if members:
        claude = detect_claude(members)
        if claude:
            rec["claude"] = claude
            return rec
        leader = members[0]
        argv = proc_argv(leader.pid)
        command, source = None, "proc"
        # trust the typed line only if the pane's own shell started what runs
        # (not e.g. a nested shell) and it was typed before that started
        if (typed and is_shell and not JOB_CONTROL_RE.match(typed)
                and any(p.ppid == pane_pid for p in members)
                and typed_at <= leader.started_at + 2):
            command, source = typed, "shell"
        if not command:
            command = proc_command(pane_pid, members)
        if command:
            rec["process"] = {
                "program": program_name(argv, leader.comm),
                "command": command,
                "source": source,
                "argv": argv,
            }
    elif typed and not JOB_CONTROL_RE.match(typed):
        rec["last_command"] = typed
    return rec


def build_snapshot():
    rows = tmux_rows("list-panes", "-a", "-F", fmt(*PANE_FIELDS))
    procs = scan_procs()
    sessions, by_sid, by_wid = [], {}, {}
    for row in rows:
        if len(row) != len(PANE_FIELDS):
            continue
        r = dict(zip(PANE_FIELDS, row))
        s = by_sid.get(r["session_id"])
        if s is None:
            s = {"name": r["session_name"], "windows": [],
                 "_last_attached": as_int(r["session_last_attached"])}
            by_sid[r["session_id"]] = s
            sessions.append(s)
        w = by_wid.get(r["window_id"])
        if w is None:
            w = {
                "index": as_int(r["window_index"]),
                "name": r["window_name"],
                "auto_rename": r["automatic-rename"] == "1",
                "layout": r["window_layout"],
                "active": r["window_active"] == "1",
                "last": r["window_last_flag"] == "1",
                "zoomed": r["window_zoomed_flag"] == "1",
                "width": as_int(r["window_width"], 80),
                "height": as_int(r["window_height"], 24),
                "panes": [],
            }
            by_wid[r["window_id"]] = w
            s["windows"].append(w)
        w["panes"].append(capture_pane(r, procs))

    # the session the user is in (most recently active client), and the one before
    active = last = None
    clients = tmux_rows("list-clients", "-F",
                        fmt("client_activity", "session_name", "client_last_session"))
    clients.sort(key=lambda c: as_int(c[0]), reverse=True)
    if clients and len(clients[0]) == 3:
        active, last = clients[0][1], clients[0][2] or None
    ordered = sorted(sessions, key=lambda s: s["_last_attached"], reverse=True)
    if active is None and ordered:
        active = ordered[0]["name"]
        last = ordered[1]["name"] if len(ordered) > 1 else None
    for s in sessions:
        del s["_last_attached"]
    return {
        "version": SNAPSHOT_VERSION,
        "saved_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "active_session": active,
        "last_session": last,
        "sessions": sessions,
    }


def is_trivial(snap):
    """Nothing worth keeping: no sessions, or one idle shell."""
    panes = [p for s in snap["sessions"] for w in s["windows"] for p in w["panes"]]
    if not panes:
        return True
    return len(panes) == 1 and not (panes[0].get("process") or panes[0].get("claude"))


def snapshots_dir():
    path = os.path.join(data_dir(), "snapshots")
    os.makedirs(path, exist_ok=True)
    return path


def list_snapshots():
    files = glob.glob(os.path.join(glob.escape(snapshots_dir()), "vrs-*.json"))
    return sorted(files, reverse=True)


def last_snapshot_path():
    return os.path.join(data_dir(), "last")


def comparable(snap):
    return {k: v for k, v in (snap or {}).items() if k != "saved_at"}


def save(auto=False):
    snap = build_snapshot()
    if auto and is_trivial(snap):
        return None, "nothing to save"
    last = last_snapshot_path()
    previous = read_json(last) if os.path.exists(last) else None
    if previous and comparable(previous) == comparable(snap):
        return os.path.realpath(last), "unchanged"
    sdir = snapshots_dir()
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    path = os.path.join(sdir, "vrs-%s.json" % stamp)
    n = 1
    while os.path.exists(path):
        path = os.path.join(sdir, "vrs-%s-%d.json" % (stamp, n))
        n += 1
    write_json_atomic(path, snap)
    tmp_link = "%s.tmp.%d" % (last, os.getpid())
    os.symlink(os.path.relpath(path, os.path.dirname(last)), tmp_link)
    os.replace(tmp_link, last)
    keep = max(1, option_int("@vrs-keep"))
    for old in list_snapshots()[keep:]:
        try:
            os.remove(old)
        except OSError:
            pass
    return path, "saved"


def describe(snap):
    sessions = snap["sessions"]
    windows = sum(len(s["windows"]) for s in sessions)
    panes = [p for s in sessions for w in s["windows"] for p in w["panes"]]
    claude = sum(1 for p in panes if p.get("claude"))
    procs = sum(1 for p in panes if p.get("process"))
    text = "%d sessions, %d windows, %d panes" % (len(sessions), windows, len(panes))
    extra = []
    if claude:
        extra.append("%d claude" % claude)
    if procs:
        extra.append("%d commands" % procs)
    if extra:
        text += " (%s)" % ", ".join(extra)
    return text


# --------------------------------------------------------------------------
# restore
# --------------------------------------------------------------------------

def existing_dir(path):
    home = os.path.expanduser("~")
    path = path or home
    while path and path != "/" and not os.path.isdir(path):
        path = os.path.dirname(path)
    return path if path and os.path.isdir(path) else home


def pane_start_dir(p):
    if p.get("claude") and p["claude"].get("cwd"):
        return existing_dir(p["claude"]["cwd"])
    return existing_dir(p.get("cwd") or p.get("path"))


def plan_pane(p, settings):
    """('run'|'ask', command) for a saved pane, or None."""
    c = p.get("claude")
    if c and settings["claude"]:
        return "run", claude_command(c)
    proc = p.get("process")
    if proc and proc.get("command"):
        if proc.get("program") in settings["autorun"]:
            return "run", proc["command"]
        if settings["ask"]:
            return "ask", proc["command"]
        return None
    if c:  # claude resume disabled: treat like any other command
        return ("ask", claude_command(c)) if settings["ask"] else None
    if settings["ask_idle"] and p.get("last_command"):
        return "ask", p["last_command"]
    return None


def split_pane(target, cwd, window):
    out_fmt = "#{pane_id}"
    try:
        return tmux("split-window", "-d", "-t", target, "-c", cwd, "-P", "-F", out_fmt).strip()
    except TmuxError:
        # no space left: even out the window and try again
        tmux_ok("select-layout", "-t", window, "tiled")
        return tmux("split-window", "-d", "-t", target, "-c", cwd, "-P", "-F", out_fmt).strip()


def restore_session(s, settings, plans):
    windows = sorted(s["windows"], key=lambda w: w["index"])
    out_fmt = fmt("session_id", "window_id", "pane_id", "window_index")
    sid = None
    active_wid = last_wid = None
    for w in windows:
        panes = sorted(w["panes"], key=lambda p: p["index"])
        if not panes:
            continue
        name_args = [] if w.get("auto_rename") else ["-n", w["name"]]
        cwd0 = pane_start_dir(panes[0])
        if sid is None:
            out = tmux("new-session", "-d", "-P", "-F", out_fmt, "-s", s["name"],
                       "-c", cwd0, "-x", w.get("width", 80), "-y", w.get("height", 24),
                       *name_args)
            sid, wid, pid0, idx = out.strip().split(SEP)
            if as_int(idx) != w["index"]:
                tmux_ok("move-window", "-s", wid, "-t", "%s:%d" % (sid, w["index"]))
        else:
            args = ["-d", "-P", "-F", out_fmt, "-c", cwd0] + name_args
            try:
                out = tmux("new-window", "-t", "%s:%d" % (sid, w["index"]), *args)
            except TmuxError:
                out = tmux("new-window", "-t", sid, *args)  # next free index
            _, wid, pid0, _ = out.strip().split(SEP)

        pane_ids = [pid0]
        for p in panes[1:]:
            pane_ids.append(split_pane(pane_ids[-1], pane_start_dir(p), wid))
        if len(pane_ids) > 1:
            if not tmux_ok("select-layout", "-t", wid, w["layout"]):
                tmux_ok("select-layout", "-t", wid, "tiled")

        active = pane_ids[0]
        for p, pid in zip(panes, pane_ids):
            if p.get("last"):
                tmux_ok("select-pane", "-t", pid)
        for p, pid in zip(panes, pane_ids):
            if p.get("active"):
                active = pid
        tmux_ok("select-pane", "-t", active)
        if w.get("zoomed") and len(pane_ids) > 1:
            tmux_ok("resize-pane", "-Z", "-t", active)

        if w.get("active"):
            active_wid = wid
        if w.get("last"):
            last_wid = wid
        for p, pid in zip(panes, pane_ids):
            plan = plan_pane(p, settings)
            if plan:
                plans.append((plan[0], pid, plan[1]))
    if last_wid:
        tmux_ok("select-window", "-t", last_wid)
    if active_wid:
        tmux_ok("select-window", "-t", active_wid)
    return sid


def list_sessions():
    rows = tmux_rows("list-sessions", "-F", fmt("session_id", "session_name", "session_windows"))
    return [(r[0], r[1], as_int(r[2])) for r in rows if len(r) == 3]


def find_pristine(sessions):
    """The lone untouched session a fresh server starts with, if that is all there is."""
    if len(sessions) != 1:
        return None
    sid, name, nwin = sessions[0]
    if nwin != 1 or not name.isdigit():
        return None
    panes = tmux_rows("list-panes", "-s", "-t", sid, "-F", fmt("pane_id", "pane_pid"))
    if len(panes) != 1:
        return None
    is_shell, members = foreground(as_int(panes[0][1]), scan_procs())
    if members or not is_shell:
        return None
    return sid, name


def restore_settings():
    return {
        "claude": option_on("@vrs-claude"),
        "autorun": set(option("@vrs-autorun").split()),
        "ask": option_on("@vrs-ask"),
        "ask_idle": option_on("@vrs-ask-idle"),
    }


def restore(path=None, auto_start=False):
    path = path or last_snapshot_path()
    snap = read_json(path)
    if not snap or "sessions" not in snap:
        if not auto_start:
            message("vrs: no snapshot to restore (%s)" % path)
        return
    if auto_start:
        # the plugin loads before tmux creates the first session; wait for it
        deadline = time.time() + 10
        while time.time() < deadline and not list_sessions():
            time.sleep(0.1)

    settings = restore_settings()
    plans = []
    restored, skipped, failed = [], [], []
    with locked("restore.lock"):
        tmux("set-option", "-g", "@vrs-restoring", "1")
        try:
            current = list_sessions()
            saved_names = {s["name"] for s in snap["sessions"]}
            pristine = find_pristine(current)
            if pristine and pristine[1] in saved_names:
                tmux_ok("rename-session", "-t", pristine[0], "vrs-%d" % os.getpid())
                current = list_sessions()
            names = {name for _, name, _ in current}
            for s in snap["sessions"]:
                if s["name"] in names:
                    skipped.append(s["name"])
                    continue
                try:
                    restore_session(s, settings, plans)
                    restored.append(s["name"])
                except TmuxError as exc:
                    failed.append("%s (%s)" % (s["name"], exc))
                    log_error("restore session %s" % s["name"])

            # leave the throwaway session the server started with
            names = {name for _, name, _ in list_sessions()}
            target = snap.get("active_session")
            if target not in names:
                target = restored[0] if restored else None
            if pristine and target and restored:
                previous = snap.get("last_session")
                clients = tmux_rows("list-clients", "-F", fmt("client_name", "session_id"))
                for client, csid in clients:
                    if csid != pristine[0]:
                        continue
                    if previous in names and previous != target:
                        tmux_ok("switch-client", "-c", client, "-t", "=" + previous)
                    tmux_ok("switch-client", "-c", client, "-t", "=" + target)
                tmux_ok("kill-session", "-t", pristine[0])

            asks = [(pane, cmd) for kind, pane, cmd in plans if kind == "ask"]
            if asks:
                with locked("pending.lock"):
                    state = load_pending()
                    for pane, cmd in asks:
                        state["pending"][pane] = cmd
                    save_pending(state)
                tmux("set-option", "-g", "@vrs-pending-any", "1")
        finally:
            tmux_ok("set-option", "-gu", "@vrs-restoring")

    focus()  # the pane the user is looking at right now
    run_now([(pane, cmd) for kind, pane, cmd in plans if kind == "run"])

    parts = ["vrs: restored %d sessions" % len(restored)]
    if skipped:
        parts.append("%d already running" % len(skipped))
    if failed:
        parts.append("failed: " + ", ".join(failed))
    if restored or failed or not auto_start:
        message(", ".join(parts))


def pane_ready(pane):
    try:
        return tmux("capture-pane", "-p", "-t", pane).strip() != ""
    except TmuxError:
        return None  # pane is gone


def send_command(pane, cmd):
    tmux("send-keys", "-t", pane, "-l", "--", cmd)
    tmux("send-keys", "-t", pane, "Enter")


def run_now(items, timeout=6.0):
    """Type commands into fresh panes once their shell has drawn a prompt."""
    deadline = time.time() + timeout
    pending = list(items)
    while pending:
        for item in list(pending):
            ready = pane_ready(item[0])
            if ready is None:
                pending.remove(item)
            elif ready or time.time() > deadline:
                try:
                    send_command(*item)
                except TmuxError:
                    pass
                pending.remove(item)
        if pending:
            time.sleep(0.1)


# --------------------------------------------------------------------------
# asking before rerunning commands
# --------------------------------------------------------------------------

def pending_path():
    return os.path.join(runtime_dir(), "pending.json")


def load_pending():
    state = read_json(pending_path(), {})
    if not isinstance(state, dict):
        state = {}
    state.setdefault("pending", {})
    state.setdefault("offers", {})
    return state


def save_pending(state):
    write_json_atomic(pending_path(), state)


def focus():
    """Offer the saved command of every pending pane a client is looking at."""
    prompts = []
    with locked("pending.lock"):
        state = load_pending()
        if not state["pending"] and not state["offers"]:
            tmux_ok("set-option", "-gu", "@vrs-pending-any")
            return
        live = set(tmux("list-panes", "-a", "-F", "#{pane_id}").split())
        for key in ("pending", "offers"):
            state[key] = {k: v for k, v in state[key].items() if k in live}
        for client, pane in tmux_rows("list-clients", "-F", fmt("client_name", "pane_id")):
            if pane in state["pending"]:
                state["offers"][pane] = state["pending"].pop(pane)
                prompts.append((client, pane, state["offers"][pane]))
        save_pending(state)
        if not state["pending"]:
            tmux_ok("set-option", "-gu", "@vrs-pending-any")
    enter = option_on("@vrs-confirm-enter")
    for client, pane, cmd in prompts:
        shown = " ".join(cmd.split())
        if len(shown) > 80:
            shown = shown[:77] + "..."
        prompt = "vrs: rerun `%s` here? (y/n)" % shown
        action = run_shell_cmd("run", pane.lstrip("%"))
        args = ["confirm-before", "-b"] + (["-y"] if enter else [])
        # confirm-before expands formats in both the prompt and the command
        tmux_ok(*args, "-t", client, "-p", prompt.replace("#", "##"), action.replace("#", "##"))


def run_offer(pane):
    pane = pane if pane.startswith("%") else "%" + pane
    with locked("pending.lock"):
        state = load_pending()
        cmd = state["offers"].pop(pane, None)
        save_pending(state)
    if cmd:
        send_command(pane, cmd)


# --------------------------------------------------------------------------
# init (called from vrs.tmux) and autosave
# --------------------------------------------------------------------------

def spawn(*args):
    subprocess.Popen(
        [sys.executable, SCRIPT] + list(args),
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True, close_fds=True,
    )


def server_age():
    _, pid = server_info()
    p = read_proc(pid)
    return time.time() - p.started_at if p else None


def init():
    save_key = option("@vrs-save-key")
    restore_key = option("@vrs-restore-key")
    if save_key:
        tmux("bind-key", save_key, "run-shell", "-b", shell_line("save"))
    if restore_key:
        tmux("bind-key", restore_key, "run-shell", "-b", shell_line("restore"))

    hook = "if -F '#{@vrs-pending-any}' { %s }" % run_shell_cmd("focus")
    for name in ("client-attached", "client-session-changed",
                 "session-window-changed", "window-pane-changed"):
        tmux("set-hook", "-g", "%s[%d]" % (name, HOOK_INDEX), hook)
    if option_on("@vrs-save-on-detach"):
        tmux("set-hook", "-g", "client-detached[%d]" % HOOK_INDEX, run_shell_cmd("save", "--auto"))
    else:
        tmux_ok("set-hook", "-gu", "client-detached[%d]" % HOOK_INDEX)

    first_load = tmux("show-option", "-gqv", "@vrs-initialized", check=False).strip() == ""
    tmux("set-option", "-g", "@vrs-initialized", "1")
    if first_load and option_on("@vrs-restore-on-start"):
        age = server_age()
        if age is not None and age < 60 and os.path.exists(last_snapshot_path()):
            spawn("restore", "--auto-start")
    if option_float("@vrs-interval") > 0:
        spawn("daemon")


def cleanup_runtime():
    base = runtime_base()
    try:
        names = os.listdir(base)
    except OSError:
        return
    for name in names:
        if not name.isdigit():
            continue
        try:
            os.kill(int(name), 0)
            continue  # server still alive
        except ProcessLookupError:
            pass
        except PermissionError:
            continue
        shutil.rmtree(os.path.join(base, name), ignore_errors=True)


def server_alive(pid):
    p = read_proc(pid)
    return p is not None and p.comm.startswith("tmux")


def daemon():
    _, server_pid = server_info()
    with locked("daemon.lock", blocking=False) as got:
        if not got:
            return  # already running for this server
        cleanup_runtime()
        last = time.time()
        while True:
            interval = option_float("@vrs-interval") * 60
            if interval <= 0:
                return
            time.sleep(max(1.0, min(15.0, interval)))
            if not server_alive(server_pid):
                return
            if time.time() - last < interval:
                continue
            last = time.time()
            if option("@vrs-restoring"):
                continue
            try:
                save(auto=True)
            except TmuxError:
                if not server_alive(server_pid):
                    return
                log_error("autosave")
            except Exception:
                log_error("autosave")


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(prog="vrs", description="save and restore tmux")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("save", help="write a snapshot")
    p.add_argument("--auto", action="store_true", help="skip trivial states, stay quiet")
    p = sub.add_parser("restore", help="restore a snapshot")
    p.add_argument("file", nargs="?", help="snapshot file (default: the latest)")
    p.add_argument("--auto-start", action="store_true", help=argparse.SUPPRESS)
    sub.add_parser("list", help="list snapshots, newest first")
    sub.add_parser("init", help=argparse.SUPPRESS)
    sub.add_parser("focus", help=argparse.SUPPRESS)
    p = sub.add_parser("run", help=argparse.SUPPRESS)
    p.add_argument("pane")
    sub.add_parser("daemon", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    try:
        if args.cmd == "save":
            path, status = save(auto=args.auto)
            if not args.auto:
                snap = read_json(path) if path else None
                message("vrs: %s %s" % (status, describe(snap)) if snap else "vrs: " + status)
        elif args.cmd == "restore":
            restore(args.file, auto_start=args.auto_start)
        elif args.cmd == "list":
            last = os.path.realpath(last_snapshot_path())
            for f in list_snapshots():
                snap = read_json(f)
                mark = "*" if os.path.realpath(f) == last else " "
                print("%s %s  %s" % (mark, f, describe(snap) if snap else "(unreadable)"))
        elif args.cmd == "init":
            init()
        elif args.cmd == "focus":
            focus()
        elif args.cmd == "run":
            run_offer(args.pane)
        elif args.cmd == "daemon":
            daemon()
    except Exception as exc:
        log_error(args.cmd)
        message("vrs: %s failed: %s" % (args.cmd, exc))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
