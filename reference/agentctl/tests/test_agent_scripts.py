"""Foreground selection and read-only inventory against isolated registrations."""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("agent_view", REPO / "scripts/ghostty-view.py")
view = importlib.util.module_from_spec(spec)
spec.loader.exec_module(view)


class AgentScripts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="agentctl-scripts-", dir="/tmp")
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "agentctl.db"
        self.con = sqlite3.connect(self.path)
        self.addCleanup(self.con.close)
        self.con.row_factory = sqlite3.Row
        self.con.executescript("""
            CREATE TABLE workers(id TEXT, kind TEXT, role TEXT, cwd TEXT, state TEXT,
                                 backend TEXT, socket TEXT, target TEXT);
            CREATE TABLE native_sessions(worker_id TEXT, pid INTEGER, process_start TEXT,
                watcher_pid INTEGER, watcher_start TEXT, stopping INTEGER);
        """)
        for wid, role, state in [("orch", "orchestrator", "idle"),
                                  ("live", "worker", "busy"),
                                  ("stale", "worker", "idle"),
                                  ("stopped", "worker", "stopped")]:
            self.con.execute("INSERT INTO workers VALUES(?, 'codex', ?, '/tmp', ?, 'native', '', '')",
                             (wid, role, state))
        start = subprocess.check_output(["ps", "-p", str(os.getpid()), "-o", "lstart="],
                                        text=True, env=dict(os.environ, LC_ALL="C")).strip()
        for wid in ("orch", "live", "stale"):
            self.con.execute("INSERT INTO native_sessions VALUES(?,?,?,?,?,0)",
                             (wid, os.getpid(), start if wid != "stale" else "old PID",
                              os.getpid(), start))
        self.con.commit()

    def test_picker_only_offers_live_native_workers(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch("builtins.input", return_value="1"):
            self.assertEqual(view.choose_worker(self.con), "live")
        self.assertIn("live", output.getvalue())
        for hidden in ("orch", "stale", "stopped"):
            self.assertNotIn(hidden, output.getvalue())

    def test_no_live_workers_does_not_prompt(self):
        self.con.execute("UPDATE native_sessions SET stopping=1")
        with patch("builtins.input") as prompt, self.assertRaisesRegex(RuntimeError, "No running"):
            view.choose_worker(self.con)
        prompt.assert_not_called()

    def test_picker_excludes_missing_watcher_and_tmux(self):
        self.con.execute("UPDATE native_sessions SET watcher_start='old PID' WHERE worker_id='live'")
        self.con.execute("INSERT INTO workers VALUES('mux', 'claude', 'worker', '/tmp', 'idle', 'tmux', 'private', 'mux')")
        with patch("builtins.input") as prompt, self.assertRaisesRegex(RuntimeError, "No running"):
            view.choose_worker(self.con)
        prompt.assert_not_called()
        worker = self.con.execute("SELECT * FROM workers WHERE id='mux'").fetchone()
        with patch.object(view.ctl, "tmux_alive", return_value=True):
            self.assertEqual(view.runtime_status(self.con, worker), "running")

    def test_selected_worker_uses_existing_attach_route(self):
        with patch.dict(os.environ, AGENTCTL_HOME=self.tmp.name), \
                patch("sys.argv", ["ghostty-view.py", "foreground", "--pick-worker"]), \
                patch("builtins.input", return_value="1"), patch.object(view.os, "execvpe") as execute, \
                contextlib.redirect_stdout(io.StringIO()):
            view.main()
        command, argv, env = execute.call_args.args
        self.assertEqual(command, str(REPO / "bin/agentctl"))
        self.assertEqual(argv, [command, "worker", "attach", "live"])
        self.assertEqual(env["AGENTCTL_HOME"], str(Path(self.tmp.name).resolve()))

    def test_inventory_lists_live_agents_aligned_without_writing(self):
        before = self.path.read_bytes()
        result = subprocess.run([str(REPO / "scripts/list-agents")], cwd="/tmp",
                                env=dict(os.environ, AGENTCTL_HOME=self.tmp.name),
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual([line.split()[0] for line in lines], ["ID", "orch", "live"])
        self.assertNotIn("\t", result.stdout)
        for column, values in {"PROVIDER": ["codex", "codex"],
                               "STATE": ["idle", "busy"], "CWD": ["/tmp", "/tmp"]}.items():
            self.assertEqual([line.index(value) for line, value in zip(lines[1:], values)],
                             [lines[0].index(column)] * 2)
        self.assertEqual(self.path.read_bytes(), before)

    def test_inventory_with_no_live_agents(self):
        self.con.execute("UPDATE native_sessions SET stopping=1")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            view.list_agents(self.con)
        self.assertEqual(output.getvalue(), "No running agents.\n")


if __name__ == "__main__":
    unittest.main()
