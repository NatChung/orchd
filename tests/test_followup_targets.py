import json
from pathlib import Path
import tempfile
import unittest

from orchd import core, store
from tests.test_orchd import FakeRuntime


class FollowupTargetsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.con = store.connect(Path(self.tmp.name) / "test.db")
        self.addCleanup(self.con.close)
        self.rt = FakeRuntime()
        self.rt.sleep = lambda _: None
        self.task = core.dispatch(self.con, self.rt, orch_thread="owner", repo="demo", title="T",
                                  instructions="do it", done_when="tests pass", model="sol",
                                  model_reason="test", task_type="code", backend="exec")
        self.target = {k: self.task[k] for k in store.ANSWER_TARGET_FIELDS}

    def queue(self):
        self.rt.alive_pids = {"4242"}
        result = core.followup(self.con, self.rt, self.task["id"], "fix the reviewed code", expected=self.target)
        self.assertEqual(result["status"], "queued")

    def test_busy_followup_resumes_only_original_session(self):
        self.queue()
        self.rt.alive_pids.clear()
        result = core.answer(self.con, self.rt, self.task["id"], flush=True)
        self.assertEqual(result["status"], "delivered")
        self.assertEqual(self.rt.resumed[0][0], self.target["session_id"])

    def test_replaced_session_cannot_consume_bound_queue(self):
        self.queue()
        store.update_task(self.con, self.task["id"], session_id="replacement")
        self.rt.alive_pids.clear()
        with self.assertRaisesRegex(ValueError, "queued followup target changed"):
            core.answer(self.con, self.rt, self.task["id"], flush=True)
        self.assertEqual(self.rt.resumed, [])
        self.assertEqual(store.pending_answer_count(self.con, self.task["id"]), 1)

    def test_retry_refused_before_stopping_original_worker(self):
        self.queue()
        with self.assertRaisesRegex(ValueError, "session-bound followups pending"):
            core.retry(self.con, self.rt, self.task["id"], "sol", "replace worker")
        self.assertEqual(self.rt.stopped, [])
        self.assertEqual(self.rt.resumed, [])
        self.assertEqual(store.get_task(self.con, self.task["id"])["session_id"], self.target["session_id"])

    def test_changed_target_is_refused_before_acceptance(self):
        store.update_task(self.con, self.task["id"], session_id="replacement")
        with self.assertRaisesRegex(ValueError, "target changed before acceptance"):
            core.followup(self.con, self.rt, self.task["id"], "fix", expected=self.target)
        self.assertEqual(store.pending_answer_count(self.con, self.task["id"]), 0)
        self.assertEqual(self.con.execute("SELECT count(*) FROM messages WHERE kind='followup'").fetchone()[0], 0)

    def test_original_failure_receipt_is_preserved(self):
        def fail(*_):
            raise RuntimeError("provider unavailable")
        self.rt.resume_codex_worker = fail
        result = core.followup(self.con, self.rt, self.task["id"], "fix", expected=self.target)
        self.assertEqual(result["status"], "failed")
        event = json.loads(self.con.execute("SELECT body FROM messages WHERE kind='followup'").fetchone()[0])
        self.assertEqual(event["status"], "failed")
        self.assertEqual(event["target"], self.target)

    def test_partial_target_is_refused(self):
        with self.assertRaisesRegex(ValueError, "complete worker identity"):
            core.followup(self.con, self.rt, self.task["id"], "fix", expected={"session_id": "old"})

    def test_generation_change_also_holds_queue(self):
        self.queue()
        store.update_task(self.con, self.task["id"], generation="new-generation")
        with self.assertRaisesRegex(ValueError, "target changed"):
            store.pending_answers(self.con, self.task["id"])

    def test_malformed_acceptance_records_hold_queue(self):
        self.queue()
        for body in ("not json", "[]"):
            self.con.execute("UPDATE messages SET body=? WHERE kind='followup'", (body,))
            with self.assertRaisesRegex(ValueError, "invalid followup queue target record"):
                store.pending_answers(self.con, self.task["id"])


if __name__ == "__main__":
    unittest.main()
