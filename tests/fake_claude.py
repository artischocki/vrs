"""Stand-in for Claude Code in the integration test.

Behaves like the real thing as far as vrs is concerned: registers itself in
$CLAUDE_CONFIG_DIR/sessions/<pid>.json, keeps a transcript under projects/,
and runs until killed. Started via tests/bin/claude with argv[0] = "claude".
"""
import json
import os
import sys
import time
import uuid

args = sys.argv[1:]
sid = args[args.index("--resume") + 1] if "--resume" in args else str(uuid.uuid4())
cfg = os.environ["CLAUDE_CONFIG_DIR"]
pid = os.getpid()
with open("/proc/self/stat") as fh:
    proc_start = fh.read().rsplit(")", 1)[1].split()[19]
cwd = os.getcwd()

os.makedirs(os.path.join(cfg, "sessions"), exist_ok=True)
with open(os.path.join(cfg, "sessions", "%d.json" % pid), "w") as fh:
    json.dump({"pid": pid, "sessionId": sid, "cwd": cwd, "procStart": proc_start,
               "kind": "interactive", "tmux": os.environ.get("TMUX_PANE")}, fh)
if "--no-transcript" not in args:
    project = os.path.join(cfg, "projects", cwd.replace("/", "-"))
    os.makedirs(project, exist_ok=True)
    with open(os.path.join(project, sid + ".jsonl"), "a") as fh:
        fh.write("{}\n")

print("fake claude, session %s" % sid, flush=True)
while True:
    time.sleep(60)
