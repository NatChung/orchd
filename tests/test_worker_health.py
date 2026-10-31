import os
import tempfile
import unittest
from unittest.mock import patch
from unittest import mock

from orchd import core, store, worker_health
from orchd.runtime import claude_job_alive
from tests.test_orchd import FakeRuntime


class ClaudeJobAliveTest(unittest.TestCase):
    def test_listed_job_is_alive_unless_failed(self):
        self.assertTrue(claude_job_alive({"j": {"state": "working"}}, "j"))
        self.assertTrue(claude_job_alive({"j": {"state": "blocked"}}, "j"))  # waiting for input, not dead
        self.assertTrue(claude_job_alive({"j": {}}, "j"))
        self.assertFalse(claude_job_alive({"j": {"state": "failed", "reapedMidWorkAt": "2026-10-01T11:04:36Z"}}, "j"))
        self.assertFalse(claude_job_alive({}, "j"))
        self.assertTrue(claude_job_alive({"j": {"state": "failed", "pid": 4242, "status": "busy"}}, "j"))
        self.assertIsNone(claude_job_alive({"j": {"state": "failed", "pid": 4242}}, "j"))

    def test_failed_query_or_missing_job_id_is_unknown(self):
        self.assertIsNone(claude_job_alive(None, "j"))
        self.assertIsNone(claude_job_alive({"j": {}}, None))


class AssessTest(unittest.TestCase):
    def test_running_or_acked_dead_without_report_is_orphan_with_manual_hint(self):
        for status in ("running", "acked"):
            got = worker_health.assess(status, False, False, task_id="t1", worktree="/wt/t1",
                                       branch="orchd/t1", base="abc")
            self.assertEqual(got["worker_health"], "orphan")
            hint = got["recovery_hint"]
            for part in ("status --porcelain", "abc..HEAD", "ls-remote", "rework_of=t1", "Do not force-remove"):
                self.assertIn(part, hint)

    def test_reported_or_finished_is_never_orphan(self):
        self.assertEqual(worker_health.assess("done", False, True)["worker_health"], "finished")
        self.assertEqual(worker_health.assess("blocked", False, True)["worker_health"], "finished")
        self.assertEqual(worker_health.assess("acked", False, True)["worker_health"], "finished")

    def test_unknown_liveness_is_not_death(self):
        got = worker_health.assess("acked", None, False)
        self.assertEqual(got, dict(worker_health="unknown", recovery_hint=None))

    def test_statuses_outside_the_check_get_no_label(self):
        for status in ("starting", "failed"):
            self.assertIsNone(worker_health.assess(status, False, False)["worker_health"])

    def test_question_with_dead_worker_is_orphan_and_unknown_stays_unknown(self):
        got = worker_health.assess("question", False, False, task_id="t1", worktree="/wt/t1", branch="b", base="abc")
        self.assertEqual(got["worker_health"], "orphan")
        self.assertIn("rework_of=t1", got["recovery_hint"])
        self.assertEqual(worker_health.assess("question", None, False)["worker_health"], "unknown")  # Codex between turns
        self.assertEqual(worker_health.assess("question", True, False)["worker_health"], "alive")

    def test_stale_is_a_label_for_watched_statuses_only(self):
        hour = 3600
        self.assertTrue(worker_health.is_stale("running", 100.0, 100.0 + 25 * hour, 24))
        self.assertFalse(worker_health.is_stale("running", 100.0, 100.0 + 23 * hour, 24))
        self.assertTrue(worker_health.is_stale("question", 100.0, 100.0 + 25 * hour, 24))
        self.assertFalse(worker_health.is_stale("done", 100.0, 100.0 + 99 * hour, 24))
        self.assertFalse(worker_health.is_stale("running", None, 100.0, 24))


class ListOpenHealthTest(unittest.TestCase):
    """Runs against a DB under a temporary ORCHD_HOME, never the real one."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.dict(os.environ, {"ORCHD_HOME": self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.con = store.connect()
        self.assertTrue(str(store.home()).startswith(self.tmp.name))
        self.rt = FakeRuntime()

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def dispatch(self, **kw):
        kw = {"model": "sonnet", "model_reason": "clear scope", "task_type": "code", "backend": "exec", **kw}
        return core.dispatch(self.con, self.rt, orch_thread="thread-A", repo="demo", title="T",
                             instructions="do it", done_when="tests pass", **kw)

    def row(self, task_id):
        return next(r for r in core.list_open(self.con, self.rt) if r["task_id"] == task_id)

    def snapshot(self):
        return ([tuple(r) for r in self.con.execute("SELECT * FROM tasks ORDER BY id")],
                [tuple(r) for r in self.con.execute("SELECT * FROM messages ORDER BY id")])

    def test_acked_task_whose_job_failed_is_orphan_and_db_is_untouched(self):
        t = self.dispatch()
        core.ack(self.con, t["id"])
        self.rt.jobs = {"job1": {"state": "failed", "reapedMidWorkAt": "2026-10-01T11:04:36.621Z"}}
        before, changes = self.snapshot(), self.con.total_changes
        row = self.row(t["id"])
        self.assertEqual((row["status"], row["worker_alive"], row["worker_health"]), ("acked", False, "orphan"))
        self.assertIn(f"rework_of={t['id']}", row["recovery_hint"])
        self.assertIn(t["worktree"], row["recovery_hint"])
        self.assertEqual(self.con.total_changes, changes)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(store.get_task(self.con, t["id"])["status"], "acked")

    def test_owner_and_worker_health_fields_coexist_and_listing_is_read_only(self):
        store.register_orch(self.con, "thread-A", "claude", job_id="owner-job", session_id="s", socket="/o.sock")
        t = self.dispatch()
        core.ack(self.con, t["id"])
        self.rt.jobs = {"job1": {"state": "working"}}
        before, changes = self.snapshot(), self.con.total_changes
        row = self.row(t["id"])
        for key in ("owner_health", "notification_delivery", "worker_health", "worker_alive", "status", "branch"):
            self.assertIn(key, row)
        self.assertEqual(row["worker_health"], "alive")
        self.assertEqual(row["owner_health"]["state"], "dead")  # owner-job is absent from the daemon
        self.assertEqual(self.con.total_changes, changes)
        self.assertEqual(self.snapshot(), before)

    def test_running_task_gone_from_daemon_is_orphan(self):
        t = self.dispatch()
        self.rt.jobs = {}
        self.assertEqual(self.row(t["id"])["worker_health"], "orphan")

    def test_live_worker_is_alive(self):
        t = self.dispatch()
        self.rt.jobs = {"job1": {"state": "working"}}
        self.assertEqual(self.row(t["id"])["worker_health"], "alive")

    def test_reported_worker_that_exited_is_not_orphan(self):
        t = self.dispatch()
        core.report(self.con, self.rt, t["id"], "done", "ok", "")
        self.rt.jobs = {}
        row = self.row(t["id"])
        self.assertEqual((row["worker_health"], row["recovery_hint"]), ("finished", None))

    def test_report_without_status_change_still_counts_as_finished(self):
        t = self.dispatch()
        store.add_message(self.con, t["id"], "report", "done: ok")  # e.g. status reset by a later answer
        self.rt.jobs = {}
        self.assertEqual(self.row(t["id"])["worker_health"], "finished")

    def test_failed_runtime_query_is_unknown(self):
        t = self.dispatch()
        self.rt.jobs = None
        row = self.row(t["id"])
        self.assertEqual((row["worker_alive"], row["worker_health"]), (None, "unknown"))

    def test_codex_between_turns_is_not_orphan(self):
        t = self.dispatch(model="sol")
        core.ask(self.con, self.rt, t["id"], "Send it?")
        self.rt.alive_pids = set()
        row = self.row(t["id"])
        self.assertIsNone(row["worker_alive"])
        self.assertNotEqual(row["worker_health"], "orphan")

    def test_codex_turn_exited_silently_is_orphan(self):
        t = self.dispatch(model="sol")
        self.rt.alive_pids = set()
        self.assertEqual(self.row(t["id"])["worker_health"], "orphan")

    def test_existing_fields_are_kept(self):
        t = self.dispatch()
        row = self.row(t["id"])
        for key in ("task_id", "repo", "title", "status", "worker_alive", "worktree", "branch", "orch_thread", "note"):
            self.assertIn(key, row)


if __name__ == "__main__":
    unittest.main()


from tests.app_support import AppBase
from unittest.mock import patch
from orchd import core, store

class AppWorkerHealthTest(AppBase):
    def test_app_states_are_not_truthy_dead_or_unknown(self):
        from orchd.worker_health import assess
        for state,expected in [("dead","orphan"),("unknown","unknown"),("active","alive"),("idle","alive"),("alive","alive")]:
            self.assertEqual(assess("running",state,False)["worker_health"],expected)

    def test_list_shows_backend_state_and_uncertain_delivery(self):
        with patch("orchd.app_worker.health",return_value="idle"):
            result=core.list_open(self.con,self.rt)[0]
        self.assertEqual(result["backend"],"app-server")
        self.assertEqual(result["worker_alive"],"idle")
        self.assertFalse(result["uncertain_delivery"])


class StaleAndLastReportTest(ListOpenHealthTest):
    """last_report_at / stale / attention_only on list_open. Time is passed in, never slept."""

    def set_times(self, task_id, when):
        self.con.execute("UPDATE messages SET created_at=? WHERE task_id=?", (when, task_id))
        self.con.execute("UPDATE tasks SET created_at=? WHERE id=?", (when, task_id))
        self.con.commit()

    def listing(self, now, **kw):
        return {r["task_id"]: r for r in core.list_open(self.con, self.rt, now=now, **kw)}

    def test_last_report_at_is_latest_worker_message_and_stale_appears_and_disappears(self):
        t = self.dispatch()
        core.ack(self.con, t["id"])
        core.progress(self.con, self.rt, t["id"], "halfway")
        self.set_times(t["id"], 1000.0)
        self.rt.jobs = {"job1": {"state": "working"}}  # worker_alive true, but silent
        row = self.listing(1000.0 + 3600)[t["id"]]
        self.assertEqual((row["last_report_at"], row["stale"], row["worker_alive"]), (1000.0, False, True))
        row = self.listing(1000.0 + 25 * 3600)[t["id"]]
        self.assertEqual((row["stale"], row["worker_health"], row["status"]), (True, "alive", "acked"))
        self.assertEqual(row["stale_after_hours"], 24)
        store.add_message(self.con, t["id"], "progress", "still going")
        self.con.execute("UPDATE messages SET created_at=? WHERE task_id=? AND body='still going'", (1000.0 + 24 * 3600, t["id"]))
        self.con.commit()
        row = self.listing(1000.0 + 25 * 3600)[t["id"]]
        self.assertEqual((row["last_report_at"], row["stale"]), (1000.0 + 24 * 3600, False))

    def test_stale_threshold_is_configurable_and_listing_never_changes_the_task(self):
        t = self.dispatch()
        core.ack(self.con, t["id"])
        self.set_times(t["id"], 1000.0)
        before = self.snapshot()
        self.assertFalse(self.listing(1000.0 + 5 * 3600)[t["id"]]["stale"])
        self.assertTrue(self.listing(1000.0 + 5 * 3600, stale_hours=4)[t["id"]]["stale"])
        with mock.patch.dict(os.environ, {"ORCHD_STALE_HOURS": "2"}):
            self.assertTrue(self.listing(1000.0 + 5 * 3600)[t["id"]]["stale"])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(store.get_task(self.con, t["id"])["status"], "acked")

    def test_task_with_no_worker_message_uses_creation_time_and_has_null_last_report(self):
        t = self.dispatch()
        self.con.execute("UPDATE tasks SET created_at=? WHERE id=?", (1000.0, t["id"]))
        self.con.execute("DELETE FROM messages WHERE task_id=? AND kind NOT IN ('ack','progress','question','report')", (t["id"],))
        self.con.commit()
        row = self.listing(1000.0 + 30 * 3600)[t["id"]]
        self.assertIsNone(row["last_report_at"])
        self.assertEqual(row["stale"], row["status"] in worker_health.WATCHED + ("question",))

    def test_question_with_dead_worker_is_orphan_in_list_open(self):
        t = self.dispatch()
        core.ask(self.con, self.rt, t["id"], "which one?")
        self.rt.jobs = {}
        row = self.listing(None)[t["id"]]
        self.assertEqual((row["status"], row["worker_alive"], row["worker_health"]), ("question", False, "orphan"))
        self.assertIn(f"rework_of={t['id']}", row["recovery_hint"])
        self.assertEqual(store.get_task(self.con, t["id"])["status"], "question")

    def test_default_lists_everything_and_attention_only_hides_healthy_running_tasks(self):
        healthy = self.dispatch()
        core.ack(self.con, healthy["id"])
        quiet = self.dispatch()
        core.ack(self.con, quiet["id"])
        asking = self.dispatch()
        core.ask(self.con, self.rt, asking["id"], "which one?")
        self.rt.jobs = {"job1": {"state": "working"}}
        self.set_times(quiet["id"], 1000.0)
        now = 1000.0 + 48 * 3600
        self.con.execute("UPDATE messages SET created_at=? WHERE task_id IN (?, ?)", (now, healthy["id"], asking["id"]))
        self.con.commit()
        self.assertEqual(set(self.listing(now)), {healthy["id"], quiet["id"], asking["id"]})  # default unchanged
        self.assertEqual(set(self.listing(now, attention_only=True)), {quiet["id"], asking["id"]})

    def test_question_with_failed_job_but_live_process_evidence_is_not_orphan(self):
        t = self.dispatch()
        core.ask(self.con, self.rt, t["id"], "which one?")
        self.rt.jobs = {"job1": {"state": "failed", "pid": 4242, "status": "idle"}}
        row = self.listing(None)[t["id"]]
        self.assertEqual((row["worker_alive"], row["worker_health"], row["recovery_hint"]), (True, "alive", None))

    def test_question_with_failed_job_and_partial_evidence_is_unknown(self):
        t = self.dispatch()
        core.ask(self.con, self.rt, t["id"], "which one?")
        for extra in ({"pid": 4242}, {"status": "idle"}, {"pid": "4242", "status": "idle"}, {"pid": 4242, "status": "newstatus"}):
            with self.subTest(extra=extra):
                self.rt.jobs = {"job1": {"state": "failed", **extra}}
                row = self.listing(None)[t["id"]]
                self.assertEqual((row["worker_alive"], row["worker_health"]), (None, "unknown"))

    def test_attention_only_keeps_health_unknown_running_tasks(self):
        t = self.dispatch()
        core.ack(self.con, t["id"])
        self.rt.jobs = None  # status probe failed
        self.assertEqual(self.listing(None)[t["id"]]["worker_health"], "unknown")
        self.assertEqual(set(self.listing(None, attention_only=True)), {t["id"]})
        self.rt.jobs = {"job1": {"state": "working"}}  # confirmed healthy: hidden again
        self.assertEqual(self.listing(None, attention_only=True), {})

    def test_attention_only_keeps_app_server_probe_failure(self):
        from orchd import app_worker
        t = self.dispatch()
        core.ack(self.con, t["id"])
        with patch("orchd.app_worker.enabled", return_value=True), patch("orchd.app_worker.health", return_value="unknown"):
            self.assertEqual(set(self.listing(None, attention_only=True)), {t["id"]})
        with patch("orchd.app_worker.enabled", return_value=True), patch("orchd.app_worker.health", return_value="idle"):
            self.assertEqual(self.listing(None, attention_only=True), {})

    def test_mcp_default_shape_is_unchanged_and_counts_are_opt_in(self):
        from orchd import mcp_server
        t = self.dispatch()
        core.ack(self.con, t["id"])
        self.rt.jobs = {"job1": {"state": "working"}}
        plain = mcp_server.call("list_open", {}, "thread-A", self.con, self.rt)
        self.assertIsInstance(plain, list)
        self.assertEqual(len(plain), 1)
        self.assertIn("last_report_at", plain[0])
        shown = mcp_server.call("list_open", {"attention_only": True, "with_counts": True}, "thread-A", self.con, self.rt)
        self.assertEqual((shown["total"], shown["shown"], shown["tasks"]), (1, 0, []))
        tool = next(x for x in mcp_server.TOOLS if x["name"] == "list_open")
        self.assertIn("PID", tool["description"])
        self.assertIn("attention_only", tool["inputSchema"]["properties"])
