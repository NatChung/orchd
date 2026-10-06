import os
import tempfile
import unittest
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
        for status in ("starting", "failed", "question"):
            self.assertIsNone(worker_health.assess(status, False, False)["worker_health"])


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
        kw = {"model": "sonnet", "model_reason": "clear scope", "task_type": "code", **kw}
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
