"""Isolated lifecycle regression tests; real processes and private tmux sockets."""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

BIN = str(Path(__file__).resolve().parents[1] / "bin" / "agentctl")
loader = importlib.machinery.SourceFileLoader("agentctl", BIN)
spec = importlib.util.spec_from_loader(loader.name, loader)
ctl = importlib.util.module_from_spec(spec)
loader.exec_module(ctl)


class Lifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="agentctl-test-")
        self.home = self.tmp.name
        self.env = patch.dict(os.environ, AGENTCTL_HOME=self.home)
        self.env.start()
        self.paths = patch.multiple(ctl, HOME=self.home, DB_PATH=self.home + "/agentctl.db",
                                    MSG_DIR=self.home + "/msgs", EVENT_LOG=self.home + "/events.jsonl")
        self.paths.start()
        self.con = ctl.db()
        self.socket = "agentctl-test-" + os.path.basename(self.home)
        self.processes = []
        self.add("orch", "orchestrator")

    def tearDown(self):
        for p in self.processes:
            if p.poll() is None:
                p.kill()
            p.wait()
        subprocess.run(["tmux", "-L", self.socket, "kill-server"], capture_output=True)
        self.con.close()
        self.paths.stop()
        self.env.stop()
        self.tmp.cleanup()

    def add(self, wid, role="worker"):
        self.con.execute("INSERT INTO workers(id,kind,role,cwd,socket,target,state,created_at) VALUES(?,?,?,?,?,?,?,?)",
                         (wid, "claude", role, str(Path(BIN).parents[1]), self.socket, wid, "idle", ctl.now()))
        self.con.commit()
        if role == "worker":
            subprocess.run(["tmux", "-L", self.socket, "new-session", "-d", "-s", wid, "sleep 120"], check=True)

    def owner(self, sid):
        p = subprocess.Popen(["sleep", "120"])
        self.processes.append(p)
        self.con.execute("BEGIN IMMEDIATE")
        token, _ = ctl.claim_orchestrator(self.con, ctl.get_worker(self.con, "orch"), sid, "claude", ctl.process_info(p.pid))
        self.con.commit()
        return p, token

    def monitor(self, token):
        p = subprocess.Popen([BIN, "watch-orchestrator", "orch", token], env=dict(os.environ))
        self.processes.append(p)
        return p

    def wait_for(self, condition):
        end = time.monotonic() + 8
        while time.monotonic() < end:
            if condition():
                return
            time.sleep(.1)
        self.fail("lifecycle condition timed out")

    def alive(self, wid):
        return ctl.tmux_alive(ctl.get_worker(self.con, wid))

    def hook(self, ev, sid, process=None, kind="claude", **extra):
        if process is None and ev != "SessionStart":
            owner = self.con.execute("SELECT pid FROM orchestrator_owner WHERE session_id=?", (sid,)).fetchone()
            process = ctl.process_info(owner[0]) if owner else None
        args = type("Args", (), {"orchestrator": True, "kind": kind})()
        payload = dict(hook_event_name=ev, session_id=sid, cwd=str(Path(BIN).parents[1]), **extra)
        out = io.StringIO()
        with patch.dict(os.environ, AGENTCTL_WORKER="orch"), patch.object(ctl, "cli_ancestor", return_value=process), \
                patch.object(ctl, "spawn_watch"), patch("sys.stdin", io.StringIO(json.dumps(payload))), contextlib.redirect_stdout(out):
            ctl.cmd_hook(args)
        return json.loads(out.getvalue())

    def test_process_death_stops_workers_but_not_unregistered_tmux(self):
        self.add("worker")
        subprocess.run(["tmux", "-L", self.socket, "new-session", "-d", "-s", "unmanaged", "sleep 120"], check=True)
        owner, token = self.owner("a")
        monitor = self.monitor(token)
        owner.kill()
        owner.wait()
        self.wait_for(lambda: not self.alive("worker"))
        self.wait_for(lambda: monitor.poll() is not None)
        self.assertIsNone(ctl.get_worker(self.con, "orch")["session_id"])
        self.assertEqual(subprocess.run(["tmux", "-L", self.socket, "has-session", "-t", "unmanaged"], capture_output=True).returncode, 0)

    def test_takeover_fences_old_monitor_and_end_hook(self):
        self.add("worker")
        a, ta = self.owner("a")
        ma = self.monitor(ta)
        b, tb = self.owner("b")
        self.monitor(tb)
        self.hook("SessionEnd", "a")
        a.terminate()
        a.wait()
        self.wait_for(lambda: ma.poll() is not None)
        self.assertTrue(self.alive("worker"))
        self.hook("SessionEnd", "b")
        self.wait_for(lambda: not self.alive("worker"))
        self.assertIsNone(b.poll(), "SessionEnd also works while CLI is still unwinding")

    def test_stop_is_not_session_end(self):
        self.add("worker")
        owner, token = self.owner("a")
        self.hook("Stop", "a")
        self.assertTrue(self.alive("worker"))
        self.assertEqual(self.con.execute("SELECT ending FROM orchestrator_owner").fetchone()[0], 0)

    def test_retired_compaction_cannot_take_back_ownership(self):
        a, _ = self.owner("a")
        b, _ = self.owner("b")
        self.assertEqual(self.hook("SessionStart", "a", ctl.process_info(a.pid), source="compact"), {})
        self.assertEqual(ctl.get_worker(self.con, "orch")["session_id"], "b")

    def test_both_registered_kinds_receive_brief(self):
        p = subprocess.Popen(["sleep", "120"])
        self.processes.append(p)
        for kind in ("claude", "codex"):
            ctl.claim_orchestrator(self.con, ctl.get_worker(self.con, "orch"), "launch-" + kind,
                                   kind, ctl.process_info(p.pid))
            self.con.commit()
            result = self.hook("SessionStart", kind, ctl.process_info(p.pid), kind=kind)
            self.assertIn("This session is the Orchestrator", result["hookSpecificOutput"]["additionalContext"])
            self.assertEqual(ctl.get_worker(self.con, "orch")["kind"], kind)

    def test_unknown_parent_cannot_claim_or_shutdown(self):
        self.hook("SessionStart", "fake")
        self.assertIsNone(ctl.get_worker(self.con, "orch")["session_id"])

    def test_delayed_first_prompt_from_superseded_launcher_cannot_take_over(self):
        old, _ = self.owner("launch-old")
        self.owner("new")
        result = self.hook("SessionStart", "actual-old-session", ctl.process_info(old.pid), source="startup")
        self.assertEqual(result, {})
        self.assertEqual(ctl.get_worker(self.con, "orch")["session_id"], "new")

    def test_resume_in_new_process_and_old_same_session_end(self):
        self.add("worker")
        old, _ = self.owner("a")
        self.owner("b")
        fresh = subprocess.Popen(["sleep", "120"])
        self.processes.append(fresh)
        # A fresh process must be explicitly claimed before resume hooks run.
        ctl.claim_orchestrator(self.con, ctl.get_worker(self.con, "orch"), "launch-fresh", "claude",
                               ctl.process_info(fresh.pid))
        self.con.commit()
        result = self.hook("SessionStart", "a", ctl.process_info(fresh.pid), source="resume")
        self.assertIn("hookSpecificOutput", result)
        self.hook("SessionEnd", "a", ctl.process_info(old.pid))
        owner = self.con.execute("SELECT * FROM orchestrator_owner").fetchone()
        self.assertEqual(owner["pid"], fresh.pid)
        self.assertEqual(owner["ending"], 0)

    def test_pid_reuse_is_detected(self):
        self.add("worker")
        owner, token = self.owner("a")
        self.con.execute("UPDATE orchestrator_owner SET process_start='not the original start'")
        self.con.commit()
        self.monitor(token)
        self.wait_for(lambda: not self.alive("worker"))
        self.assertIsNone(owner.poll())

    def test_interrupts_pending_work_preserves_reports(self):
        self.add("worker")
        _, token = self.owner("a")
        for mid, state, recipient, kind in (("one", "acked", "worker", "dispatch"),
                                           ("two", "queued", "worker", "dispatch"),
                                           ("reply", "queued", "orchestrator", "reply")):
            self.con.execute("INSERT INTO messages(id,sender,recipient,kind,summary,state,created_at) VALUES(?,?,?,?,?,?,?)",
                             (mid, "orchestrator", recipient, kind, "test", state, ctl.now()))
        ctl.shutdown_orchestrator(self.con, "orch", token)
        self.con.commit()
        states = dict(self.con.execute("SELECT id,state FROM messages"))
        self.assertEqual(states["one"], "interrupted")
        self.assertEqual(states["two"], "interrupted")
        self.assertEqual(states["reply"], "queued")
        # A delayed pump holding a stale worker row cannot deliver after cleanup.
        self.assertEqual(ctl.pump_worker(self.con, {"id": "worker", "state": "idle"}), "worker stopped")
        self.con.commit()

    def test_config_install_preserves_other_handlers_and_is_idempotent(self):
        for kind, rel in (("claude", ".claude/settings.local.json"), ("codex", ".codex/hooks.json")):
            path = Path(self.home) / rel
            path.parent.mkdir()
            path.write_text(json.dumps({"custom": True, "hooks": {"SessionStart": [{"matcher": "startup", "hooks": [
                {"type": "command", "command": "echo keep"}, {"type": "command", "command": BIN + " hook"}]}]}}))
            w = dict(id="orch", kind=kind, role="orchestrator", cwd=self.home)
            ctl.write_hook_config(w)
            first = path.read_text()
            ctl.write_hook_config(w)
            self.assertEqual(first, path.read_text())
            cfg = json.loads(first)
            self.assertTrue(cfg["custom"])
            self.assertEqual(cfg["hooks"]["SessionStart"][0]["hooks"][0]["command"], "echo keep")
            self.assertIn("SessionEnd", cfg["hooks"])


if __name__ == "__main__":
    unittest.main()
