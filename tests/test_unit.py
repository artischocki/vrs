"""Unit tests for the pure parts of vrs.py.  Run: python3 -m unittest discover tests"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import vrs  # noqa: E402

SETTINGS = {"claude": True, "autorun": {"nvim"}, "ask": True, "ask_idle": False}


class ClaudeArgs(unittest.TestCase):
    def keep(self, line):
        return vrs.claude_keep_args(line.split())

    def test_keeps_session_settings(self):
        self.assertEqual(self.keep("--dangerously-skip-permissions --model opus"),
                         ["--dangerously-skip-permissions", "--model", "opus"])
        self.assertEqual(self.keep("--model=sonnet --effort high"),
                         ["--model=sonnet", "--effort", "high"])
        self.assertEqual(self.keep("--add-dir ../a ../b --permission-mode plan"),
                         ["--add-dir", "../a", "../b", "--permission-mode", "plan"])

    def test_drops_session_selection_and_prompt(self):
        self.assertEqual(self.keep("-c"), [])
        self.assertEqual(self.keep("--resume 1234 --model opus"), ["--model", "opus"])
        self.assertEqual(self.keep("-r --model opus"), ["--model", "opus"])
        self.assertEqual(self.keep("--session-id abc fix-the-bug"), [])
        self.assertEqual(self.keep("-n name --fork-session"), [])
        self.assertEqual(self.keep("--model opus -- --ide"), ["--model", "opus"])

    def test_drops_unknown_flags(self):
        self.assertEqual(self.keep("--future-flag value --ide"), ["--ide"])

    def test_session_from_argv(self):
        sid = "e0ae78c1-0350-4066-bdfd-e6ebf37385ed"
        self.assertEqual(vrs.claude_from_argv(["claude", "--resume", sid]), sid)
        self.assertEqual(vrs.claude_from_argv(["claude", "--session-id=" + sid]), sid)
        self.assertIsNone(vrs.claude_from_argv(["claude", "-r", "my-session-name"]))


class Commands(unittest.TestCase):
    def test_argv_to_command(self):
        self.assertEqual(vrs.argv_to_command(["python3", "-m", "http.server", "8000"]),
                         "python3 -m http.server 8000")
        self.assertEqual(vrs.argv_to_command(["grep", "a b", "file"]), "grep 'a b' file")
        self.assertEqual(vrs.argv_to_command(["./run.sh"]), "./run.sh")
        self.assertEqual(vrs.argv_to_command(["/usr/bin/env", "true"]), "env true")
        # a process that rewrote its title into one string
        self.assertEqual(vrs.argv_to_command(["npm run dev"]), "npm run dev")

    def test_program_name(self):
        self.assertEqual(vrs.program_name(["/snap/nvim/4898/usr/bin/nvim", "f"]), "nvim")
        self.assertEqual(vrs.program_name(["-zsh"]), "zsh")
        self.assertEqual(vrs.program_name([], "comm"), "comm")

    def test_job_control_is_not_a_command(self):
        for line in ("fg", "fg %1", "%2", "bg", "jobs", " %vim"):
            self.assertTrue(vrs.JOB_CONTROL_RE.match(line), line)
        for line in ("make", "fgrep foo", "jobsctl", "vim %"):
            self.assertFalse(vrs.JOB_CONTROL_RE.match(line), line)

    def test_bash_history_prefix(self):
        self.assertEqual(vrs.HIST_PREFIX_RE.sub("", "  42  make test", count=1), "make test")
        self.assertEqual(vrs.HIST_PREFIX_RE.sub("", " 7* vim x", count=1), "vim x")


class Planning(unittest.TestCase):
    def test_nvim_runs_others_ask(self):
        nv = {"process": {"program": "nvim", "command": "nvim a.txt"}}
        dev = {"process": {"program": "node", "command": "npm run dev"}}
        self.assertEqual(vrs.plan_pane(nv, SETTINGS), ("run", "nvim a.txt"))
        self.assertEqual(vrs.plan_pane(dev, SETTINGS), ("ask", "npm run dev"))
        self.assertIsNone(vrs.plan_pane(dev, dict(SETTINGS, ask=False)))

    def test_idle_shell(self):
        idle = {"last_command": "make test"}
        self.assertIsNone(vrs.plan_pane(idle, SETTINGS))
        self.assertEqual(vrs.plan_pane(idle, dict(SETTINGS, ask_idle=True)), ("ask", "make test"))
        self.assertIsNone(vrs.plan_pane({}, dict(SETTINGS, ask_idle=True)))

    def test_trivial(self):
        one = {"sessions": [{"windows": [{"panes": [{"cwd": "/"}]}]}]}
        self.assertTrue(vrs.is_trivial(one))
        self.assertTrue(vrs.is_trivial({"sessions": []}))
        busy = {"sessions": [{"windows": [{"panes": [{"process": {"command": "htop"}}]}]}]}
        self.assertFalse(vrs.is_trivial(busy))

    def test_tmux_quote(self):
        self.assertEqual(vrs.tmux_quote('a "b" $c \\d'), '"a \\"b\\" \\$c \\\\d"')


if __name__ == "__main__":
    unittest.main()
