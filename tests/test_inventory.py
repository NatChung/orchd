import contextlib
import io
import json
import os
import runpy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from orchd import inventory, mcp_server, store


class InventoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "orchd.db"
        self.con = store.connect(self.path)
        self.addCleanup(self.con.close)
        self.rt = Mock()
        self.rt.live_jobs.return_value = {"claude-job": {}}

    def register(self):
        store.register_orch(self.con, "claude", "claude", job_id="claude-job")
        store.register_orch(self.con, "codex", "codex")

    def task(self, id, owner, status):
        store.create_task(self.con, id=id, repo="demo", repo_path="/demo", title="PRIVATE",
                          instructions="PRIVATE", done_when="PRIVATE", orch_thread=owner,
                          codex_bin="codex", status=status, job_id="worker-job")

    def test_zero_tasks_stopped_alive_and_counts_without_writes(self):
        self.register()
        store.stop_orch(self.con, "claude")
        self.task("open", "claude", "done")
        self.task("closed", "codex", "closed")
        before = list(self.con.iterdump())
        out = inventory.list_orchs(self.con, self.rt)
        rows = {r["orch_id"]: r for r in out["orchs"]}
        self.assertEqual(rows["codex"]["open_task_count"], 0)
        self.assertEqual(rows["claude"]["open_task_count"], 1)
        self.assertIsNotNone(rows["claude"]["stopped_at"])
        self.assertEqual(rows["claude"]["health"], "alive")
        self.assertEqual(rows["codex"]["health"], "unknown")
        self.assertEqual(out["counts"], dict(registered=2, alive=1, dead=0, unknown=1))
        self.assertEqual(before, list(self.con.iterdump()))
        self.assertNotIn("PRIVATE", json.dumps(out))
        self.rt.live_jobs.assert_called_once_with()

    def test_unavailable_invalid_and_dead(self):
        self.register()
        for jobs in (None, [], {None: {}}, {"bad": []}):
            with self.subTest(jobs=jobs):
                self.rt.live_jobs.return_value = jobs
                out = inventory.list_orchs(self.con, self.rt)
                self.assertEqual(out["counts"]["unknown"], 2)
                self.assertIsNone(out["unidentified_claude_sessions"])
        self.rt.live_jobs.side_effect = RuntimeError("PRIVATE")
        self.assertEqual(inventory.list_orchs(self.con, self.rt)["counts"]["unknown"], 2)
        self.rt.live_jobs.side_effect = None
        self.rt.live_jobs.return_value = {}
        self.assertEqual(inventory.list_orchs(self.con, self.rt)["counts"],
                         dict(registered=2, alive=0, dead=1, unknown=1))

    def test_unidentified_sessions_exclude_known_workers_and_dead(self):
        self.register()
        self.task("closed", "codex", "closed")
        self.rt.live_jobs.return_value = {"claude-job": {}, "worker-job": {},
                                          "unknown": {"prompt": "PRIVATE"},
                                          "failed": {"state": "failed"}}
        self.assertEqual(inventory.list_orchs(self.con, self.rt)["unidentified_claude_sessions"],
                         [dict(job_id="unknown", health="alive")])

    def test_empty_registry(self):
        self.rt.live_jobs.return_value = {}
        self.assertEqual(inventory.list_orchs(self.con, self.rt),
                         dict(orchs=[], counts=dict(registered=0, alive=0, dead=0, unknown=0),
                              unidentified_claude_sessions=[]))

    def test_mcp_does_not_register_caller_or_write(self):
        self.register()
        before = list(self.con.iterdump())
        with patch.dict(os.environ, {"ORCHD_ORCH_ID": ""}):
            reply = mcp_server.handle(dict(id=1, method="tools/call", params=dict(
                name="list_orchs", arguments={}, _meta=dict(threadId="new-caller"))), self.con, self.rt)
        data = json.loads(reply["result"]["content"][0]["text"])
        self.assertEqual(data["counts"]["registered"], 2)
        self.assertEqual(before, list(self.con.iterdump()))
        self.assertIn("list_orchs", {t["name"] for t in mcp_server.TOOLS})

    def test_cli_snapshot_leaves_source_files_unchanged(self):
        self.register()
        def source():
            return {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in Path(self.tmp.name).iterdir()
                    if p.is_file()}
        before = source()
        cli = runpy.run_path(str(Path(__file__).resolve().parents[1] / "bin/orchd"))
        output = io.StringIO()
        with patch.dict(os.environ, {"ORCHD_HOME": self.tmp.name}), patch(
                "orchd.runtime.Runtime.live_jobs", return_value={"claude-job": {}}), contextlib.redirect_stdout(output):
            self.assertEqual(cli["main"](["orchs"]), 0)
        self.assertEqual(json.loads(output.getvalue())["counts"]["registered"], 2)
        self.assertEqual(before, source())

    def test_cli_missing_db_does_not_create_home(self):
        cli = runpy.run_path(str(Path(__file__).resolve().parents[1] / "bin/orchd"))
        missing = Path(self.tmp.name) / "absent"
        with patch.dict(os.environ, {"ORCHD_HOME": str(missing)}), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli["main"](["orchs"]), 1)
        self.assertFalse(missing.exists())
