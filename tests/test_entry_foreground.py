"""Foreground uses temporary state and fake viewers; no live windows or workers."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from orchd import core, entry, inventory, mcp_server, store
from orchd.runtime import WORKER_MODELS
from tests.test_entry import EntryRuntime


class EntryForegroundTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.con = store.connect(Path(self.tmp.name) / "state.db")
        self.addCleanup(self.con.close)
        self.rt = EntryRuntime()
        self.orch = core.start_orch(self.con, self.rt, "opus")["id"]
        entry.bind(self.con, self.rt, self.orch)
        self.rt.open_app_viewer = Mock()

    def call(self, args):
        return mcp_server.handle({"id": 1, "method": "tools/call", "params": {
            "name": "foreground", "arguments": args, "_meta": {"threadId": "desktop-thread"}}},
            self.con, self.rt, role="entry")["result"]

    def success(self, args):
        result = self.call(args)
        self.assertNotIn("isError", result)
        data = result["structuredContent"]
        self.assertEqual(data, json.loads(result["content"][0]["text"]))
        self.assertEqual(data["launch_status"], "launch_requested")
        self.assertIsNone(data["window_opened"])
        self.assertEqual(data["orch_id"], self.orch)
        return data

    def task(self, *, owner=None, model="sonnet", backend="exec"):
        return store.create_task(self.con, id="abcdef12", repo="demo", repo_path="/demo", title="T",
                                 instructions="I", done_when="D", orch_thread=owner or self.orch, codex_bin="codex",
                                 model=WORKER_MODELS[model], backend=backend, job_id="4242" if model == "sol" else "job1",
                                 worktree="/fake/worktree", session_id="thread-W", generation="generation-1",
                                 status="running")

    def test_rejects_every_other_argument_shape_before_launch(self):
        invalid = [{}, None, [], "orch", {"target": "worker"}, {"target": 1},
                   {"target": "orch", "task_id": "abcdef12"}, {"orch_id": self.orch},
                   {"task_id": "short"}, {"task_id": "abcdef12\n"}, {"task_id": "zzzzzzzz"},
                   {"task_id": 12345678}, {"task_id": None}]
        for extra in ("command", "path", "socket", "url", "unknown"):
            invalid.extend([{"target": "orch", extra: "x"}, {"task_id": "abcdef12", extra: "x"}])
        with patch.object(inventory, "attach") as attach, patch.object(core, "view") as view:
            for args in invalid:
                with self.subTest(args=args):
                    self.assertTrue(self.call(args)["isError"])
            attach.assert_not_called()
            view.assert_not_called()

    def test_orch_delegates_to_inventory_attach_with_viewer(self):
        with patch.object(inventory, "attach", wraps=inventory.attach) as attach:
            data = self.success({"target": "orch"})
        attach.assert_called_once_with(self.con, self.rt, self.orch, viewer=True)
        self.assertEqual(self.rt.viewed, ["orchjob"])
        self.assertEqual(data["target"], "orch")
        self.assertEqual(data["kind"], "claude")
        self.assertEqual(data["launched"], {"viewer": "claude attach", "job_id": "orchjob"})

    def test_retired_orch_revives_same_conversation_without_stdout_pollution(self):
        self.rt.jobs = {}
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch.object(self.rt, "socket_listening", side_effect=[False, True]):
            data = self.success({"target": "orch"})
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(self.rt.resumed_orchs, [(self.orch, "claude-opus-5-5", "orchsession")])
        self.assertEqual(data["launched"]["job_id"], "resumed1")

    def test_unbound_unknown_and_foreign_task_never_launch(self):
        with patch.object(core, "view") as view, patch.object(inventory, "attach") as attach:
            self.assertIn("unknown task", self.call({"task_id": "abcdef12"})["content"][0]["text"])
            self.task(owner="other-orch")
            self.assertIn("does not belong", self.call({"task_id": "abcdef12"})["content"][0]["text"])
            self.con.execute("DELETE FROM entries")
            for args in ({"target": "orch"}, {"task_id": "abcdef12"}):
                self.assertIn("not bound", self.call(args)["content"][0]["text"])
            view.assert_not_called()
            attach.assert_not_called()

    def test_current_owner_after_adoption_is_required(self):
        self.task()
        store.update_task(self.con, "abcdef12", orch_thread="other-orch")
        self.assertTrue(self.call({"task_id": "abcdef12"})["isError"])
        store.update_task(self.con, "abcdef12", orch_thread=self.orch)
        self.success({"task_id": "ABCDEF12"})
        self.assertEqual(self.rt.viewed, ["job1"])

    def test_claude_worker_routes_to_attach(self):
        self.task()
        with patch.object(core, "view", wraps=core.view) as view:
            data = self.success({"task_id": "abcdef12"})
        view.assert_called_once_with(self.con, self.rt, "abcdef12")
        self.assertEqual(self.rt.viewed, ["job1"])
        self.assertEqual(data["kind"], "claude")
        self.assertEqual(data["launched"], {"viewer": "claude attach", "job_id": "job1"})

    def test_app_server_routes_to_native_remote_tui_while_busy_or_idle(self):
        self.task(model="sol", backend="app-server")
        self.rt.alive_pids.add("4242")
        for health in ("active", "idle"):
            with self.subTest(health=health), patch.object(core.app_worker, "health", return_value=health):
                data = self.success({"task_id": "abcdef12"})
            db, task = self.rt.open_app_viewer.call_args.args
            self.assertEqual(Path(db), (Path(self.tmp.name) / "state.db").resolve())
            self.assertEqual(task["id"], "abcdef12")
            self.assertEqual(data["kind"], "codex-app-server")
            self.assertEqual(data["launched"], {"viewer": "native remote TUI", "session_id": "thread-W",
                                                "generation": "generation-1"})
        self.assertEqual(self.rt.viewed, [])

    def test_exec_busy_refuses_then_resumes_between_turns(self):
        self.task(model="sol")
        self.rt.alive_pids.add("4242")
        result = self.call({"task_id": "abcdef12"})
        self.assertTrue(result["isError"])
        self.assertIn("codex worker is in a turn", result["content"][0]["text"])
        self.assertEqual(self.rt.viewed, [])
        self.rt.alive_pids.clear()
        data = self.success({"task_id": "abcdef12"})
        self.assertEqual(data["kind"], "codex-exec")
        self.assertEqual(data["launched"], {"viewer": "codex resume", "session_id": "thread-W"})
        self.assertEqual(self.rt.viewed, [("codex", "thread-W")])

    def test_launch_failure_is_an_error_not_a_window_receipt(self):
        self.task()
        with patch.object(self.rt, "open_viewer", side_effect=OSError("Ghostty unavailable")):
            result = self.call({"task_id": "abcdef12"})
        self.assertTrue(result["isError"])
        self.assertIn("Ghostty unavailable", result["content"][0]["text"])
        self.assertNotIn("structuredContent", result)
