import contextlib
import io
import json
import os
import runpy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchd import core, mcp_server, store
from orchd.orch_health import owner_health
from tests.test_orchd import FakeRuntime


class OrchHealthTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"ORCHD_HOME": self.tmp.name})
        self.env.start()
        self.con = store.connect()
        self.rt = FakeRuntime()

    def tearDown(self):
        self.con.close()
        self.env.stop()
        self.tmp.cleanup()

    def task(self, owner="owner-A", **kwargs):
        return core.dispatch(self.con, self.rt, orch_thread=owner, repo="demo", title="T",
                             instructions="do it", done_when="tests pass", model_reason="clear scope",
                             task_type="code", **kwargs)

    def owner(self, owner="owner-A", kind="claude", job="owner-job"):
        return store.register_orch(self.con, owner, kind, job_id=job, session_id="owner-session",
                                   socket="/private/owner.sock")

    def test_missing_owner_and_unread_failed_wakes_are_visible_without_consumption(self):
        self.owner()
        t = self.task()
        with patch.object(self.rt, "send_uds", side_effect=FileNotFoundError("SECRET-token /private/socket")):
            self.assertFalse(core.progress(self.con, self.rt, t["id"], "PRIVATE-progress-body"))
            self.assertFalse(core.report(self.con, self.rt, t["id"], "done", "PRIVATE-report", "PRIVATE-evidence"))
        before = list(self.con.iterdump())
        for _ in range(2):
            row, = core.list_open(self.con, self.rt)
            self.assertIn("owner_health", row)
            self.assertEqual(row["owner_health"], {"state": "dead", "reason": "job_absent"})
            self.assertTrue(row["worker_alive"])
            delivery = row["notification_delivery"]
            self.assertEqual(delivery["unread_count"], 2)
            self.assertEqual(delivery["unread_wake_failed_count"], 2)
            latest = delivery["latest_unread_wake_failure"]
            self.assertEqual(latest["kind"], "report")
            self.assertEqual(latest["error_type"], "FileNotFoundError")
            self.assertIsInstance(latest["message_id"], int)
            self.assertIsInstance(latest["created_at"], float)
            for private in ("PRIVATE", "SECRET", "/private/", "owner-session"):
                self.assertNotIn(private, json.dumps(row))
        self.assertEqual(list(self.con.iterdump()), before)
        self.assertEqual(len(store.unread_for_thread(self.con, "owner-A")), 2)

    def test_owner_live_dead_and_unknown_are_independent_of_worker(self):
        self.owner()
        self.task()
        for jobs, state, reason, worker in (
            ({"owner-job": {}}, "alive", "job_present", False),
            ({"job1": {}}, "dead", "job_absent", True),
            (None, "unknown", "runtime_unavailable", None),
        ):
            with self.subTest(state=state):
                self.rt.jobs = jobs
                row, = core.list_open(self.con, self.rt)
                self.assertEqual(row["owner_health"], {"state": state, "reason": reason})
                self.assertIs(row["worker_alive"], worker)

    def test_codex_unregistered_missing_job_and_unsupported_owner_are_unknown(self):
        for owner, kind, job, reason in (
            ("codex", "codex", "not-a-claude-job", "codex_unverified"),
            ("legacy", None, None, "owner_unregistered"),
            ("no-job", "claude", None, "job_missing"),
            ("future", "future", "owner-job", "kind_unsupported"),
        ):
            if kind:
                self.owner(owner, kind, job)
            self.task(owner)
        self.rt.jobs = {}
        rows = {r["orch_thread"]: r for r in core.list_open(self.con, self.rt)}
        for owner, reason in (("codex", "codex_unverified"), ("legacy", "owner_unregistered"),
                              ("no-job", "job_missing"), ("future", "kind_unsupported")):
            self.assertEqual(rows[owner]["owner_health"], {"state": "unknown", "reason": reason})

    def test_runtime_query_exception_is_unknown_and_does_not_leak_error(self):
        self.owner()
        self.task()
        with patch.object(self.rt, "live_jobs", side_effect=RuntimeError("SECRET-runtime-error")):
            row, = core.list_open(self.con, self.rt)
        self.assertEqual(row["owner_health"], {"state": "unknown", "reason": "runtime_unavailable"})
        self.assertIsNone(row["worker_alive"])
        self.assertNotIn("SECRET", json.dumps(row))

    def test_malformed_runtime_snapshot_never_proves_owner_dead(self):
        owner = self.owner()
        for jobs in ([], "unknown", {"other-job": None}, {None: {}}):
            with self.subTest(jobs=jobs):
                self.assertEqual(owner_health(owner, jobs),
                                 {"state": "unknown", "reason": "runtime_invalid"})

    def test_listed_failed_owner_job_is_dead_but_other_listed_states_are_not(self):
        owner = self.owner()
        failed = {owner["job_id"]: {"state": "failed", "reapedMidWorkAt": "2026-10-01T11:04:36Z"}}
        self.assertEqual(owner_health(owner, failed), {"state": "dead", "reason": "job_failed"})
        for entry in ({"state": "working"}, {"state": "blocked"}, {"state": "done", "status": "idle"},
                      {"state": "somethingnew"}, {}):
            with self.subTest(entry=entry):  # idle/done live jobs and unknown states are never guessed dead
                self.assertEqual(owner_health(owner, {owner["job_id"]: entry}),
                                 {"state": "alive", "reason": "job_present"})
        self.assertEqual(owner_health(owner, None), {"state": "unknown", "reason": "runtime_unavailable"})
        self.assertEqual(owner_health(owner, {"other": {"state": "failed"}}),
                         {"state": "dead", "reason": "job_absent"})

    def test_stopped_at_is_bookkeeping_not_liveness_proof_and_is_preserved(self):
        self.owner()
        self.task()
        store.stop_orch(self.con, "owner-A")
        before = list(self.con.iterdump())
        for jobs, state in ((None, "unknown"), ({"owner-job": {}}, "alive"), ({}, "dead")):
            self.rt.jobs = jobs
            row, = core.list_open(self.con, self.rt)
            self.assertEqual(row["owner_health"]["state"], state)
        self.assertEqual(list(self.con.iterdump()), before)

    def test_delivery_counts_only_unread_inbox_messages_for_each_task(self):
        a, b = self.task(), self.task("owner-B")
        core.ack(self.con, a["id"])
        core.progress(self.con, self.rt, a["id"], "PRIVATE-success")
        self.rt.wake_fails = True
        core.ask(self.con, self.rt, a["id"], "PRIVATE-question")
        core.progress(self.con, self.rt, b["id"], "PRIVATE-other-group")
        ignored = store.add_message(self.con, a["id"], "answer", "PRIVATE-answer")
        self.con.execute("UPDATE messages SET wake_error='RuntimeError: SECRET' WHERE id=?", (ignored,))
        old = store.add_message(self.con, a["id"], "report", "PRIVATE-old-report")
        self.con.execute("UPDATE messages SET wake_error='RuntimeError: SECRET',read_at=1 WHERE id=?", (old,))
        before = list(self.con.iterdump())
        # MCP list_open spans groups but must not make other groups' messages public.
        rows = mcp_server.call("list_open", {}, "owner-C", self.con, self.rt)
        by_task = {r["task_id"]: r["notification_delivery"] for r in rows}
        self.assertEqual(by_task[a["id"]]["unread_count"], 3)
        self.assertEqual(by_task[a["id"]]["unread_wake_failed_count"], 1)
        self.assertEqual(by_task[a["id"]]["latest_unread_wake_failure"]["kind"], "question")
        self.assertEqual(by_task[b["id"]]["unread_count"], 1)
        self.assertEqual(by_task[b["id"]]["unread_wake_failed_count"], 1)
        self.assertNotIn("PRIVATE", json.dumps(rows))
        self.assertNotIn("SECRET", json.dumps(rows))
        self.assertEqual(list(self.con.iterdump()), before)
        self.assertEqual(len(core.inbox(self.con, "owner-A")), 3)
        self.assertEqual(len(core.inbox(self.con, "owner-B")), 1)
        for r in core.list_open(self.con, self.rt):
            self.assertEqual(r["notification_delivery"], dict(
                unread_count=0, unread_wake_failed_count=0, latest_unread_wake_failure=None))

    def test_error_type_is_allowlisted_even_when_prefix_contains_private_text(self):
        t = self.task()
        for raw, expected in (("SECRET-token: PRIVATE-error", "unknown"),
                              ("FileNotFoundError: SECRET-token", "FileNotFoundError"),
                              ("CalledProcessError: PRIVATE-command", "CalledProcessError"),
                              ("PRIVATE-error-without-colon", "unknown"),
                              ("", "unknown")):
            with self.subTest(raw=raw):
                mid = store.add_message(self.con, t["id"], "progress", "PRIVATE-body")
                self.con.execute("UPDATE messages SET wake_error=? WHERE id=?", (raw, mid))
                row, = core.list_open(self.con, self.rt)
                failure = row["notification_delivery"]["latest_unread_wake_failure"]
                self.assertEqual(failure["error_type"], expected)
                self.assertNotIn("PRIVATE", json.dumps(row))
                self.assertNotIn("SECRET", json.dumps(row))

    def test_original_fields_remain_compatible_and_cli_list_exposes_health(self):
        t = self.task()
        row, = core.list_open(self.con, self.rt)
        self.assertEqual({k: row[k] for k in (
            "task_id", "repo", "title", "status", "worker_alive", "worktree", "branch", "orch_thread", "note"
        )}, dict(task_id=t["id"], repo="demo", title="T", status="running", worker_alive=True,
                 worktree=t["worktree"], branch=t["branch"], orch_thread="owner-A", note=None))
        cli = runpy.run_path(str(Path(__file__).resolve().parents[1] / "bin" / "orchd"))
        output = io.StringIO()
        with patch.object(store, "connect", return_value=self.con), \
                patch.dict(cli["main"].__globals__, {"Runtime": lambda: self.rt}), \
                contextlib.redirect_stdout(output):
            self.assertEqual(cli["main"](["list"]), 0)
        self.assertEqual(json.loads(output.getvalue()), [row])
        self.assertEqual(row["owner_health"]["state"], "unknown")

    def test_cli_runtime_failure_keeps_unread_delivery_evidence(self):
        self.owner()
        t = self.task()
        with patch.object(self.rt, "send_uds", side_effect=FileNotFoundError("PRIVATE-socket")):
            core.progress(self.con, self.rt, t["id"], "PRIVATE-progress")
        before = list(self.con.iterdump())
        cli = Path(__file__).resolve().parents[1] / "bin" / "orchd"
        result = subprocess.run([sys.executable, str(cli), "list"], capture_output=True, text=True,
                                timeout=10, check=True, env={**os.environ,
                                "ORCHD_CLAUDE": str(Path(self.tmp.name) / "missing-claude")})
        row, = json.loads(result.stdout)
        self.assertEqual(row["owner_health"], {"state": "unknown", "reason": "runtime_unavailable"})
        self.assertEqual(row["notification_delivery"]["unread_wake_failed_count"], 1)
        self.assertNotIn("PRIVATE", result.stdout)
        self.assertEqual(list(self.con.iterdump()), before)


if __name__ == "__main__":
    unittest.main()
