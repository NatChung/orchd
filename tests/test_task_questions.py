import json
import tempfile
import unittest
from pathlib import Path

from orchd import core, snapshot, store, task_questions


class PendingWorkerQuestionsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "fixture.db"
        self.con = store.connect(self.path)
        self.addCleanup(self.con.close)

    def task(self, tid="task", status="question", owner="owner-a"):
        self.con.execute("INSERT INTO tasks(id,repo,repo_path,title,instructions,done_when,orch_thread,codex_bin,"
                         "status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                         (tid, "synthetic", "/synthetic", "Synthetic task", "instruction", "check", owner,
                          "codex", status, 1, 2))
        return store.get_task(self.con, tid)

    def message(self, tid, kind="question", body="Synthetic question", read_at=None, followup_id=None):
        mid = store.add_message(self.con, tid, kind, body,
                                evidence=json.dumps(dict(followup_id=followup_id)) if followup_id else None)
        self.con.execute("UPDATE messages SET read_at=? WHERE id=?", (read_at, mid))
        return mid

    def rows(self):
        return snapshot.read(self.path, schema_version=2)["pending_worker_questions"]

    def test_eight_reported_ids_use_only_synthetic_fixture_and_survive_reads_across_owners(self):
        ids = ("d41feef1", "af1ed4ac", "0bc31fcc", "dfb8a30d", "9205e391", "39d8c8b2", "1ebd2b7f", "cbb7c100")
        expected = {}
        for i, tid in enumerate(ids):
            self.task(tid, owner=f"synthetic-owner-{i % 2}")
            self.message(tid, body="Older synthetic question")
            expected[tid] = self.message(tid, read_at=10 if i % 2 else None)
        rows = self.rows()
        self.assertEqual({r["task_id"]: r["id"] for r in rows}, expected)
        self.assertEqual({r["task"]["owner"] for r in rows}, {"synthetic-owner-0", "synthetic-owner-1"})
        for row in rows:
            self.assertEqual(row["body"], "Synthetic question")
            self.assertEqual(row["processing"], "awaiting_decision")
            self.assertFalse(row["evidence"]["read_at_affects_pending"])
            self.assertTrue(row["task_open"])
            self.assertEqual(row["task"]["id"], row["task_id"])
            self.assertEqual(row["task"]["orch_thread"], row["task"]["owner"])
        data = snapshot.read(self.path, schema_version=2)
        self.assertEqual(len(data["worker_questions"]), 12)
        self.assertNotIn("pending_worker_questions", snapshot.read(self.path, schema_version=1))

    def test_closed_and_answered_excluded_queue_saved_and_new_question_pending(self):
        for tid, status in (("closed", "closed"), ("answered", "acked"), ("queued", "question"),
                            ("consumed", "acked"), ("new", "question"), ("missing", "question"), ("empty", "done")):
            self.task(tid, status)
            if tid not in ("missing", "empty"):
                self.message(tid)
        self.message("answered", "answer")
        queued = self.message("queued", "answer_queued")
        self.message("consumed", "answer_queued", read_at=20)
        self.message("new", "answer")
        new = self.message("new")
        rows = {r["task_id"]: r for r in self.rows()}
        self.assertEqual(set(rows), {"queued", "new", "missing"})
        self.assertEqual(rows["queued"]["processing"], "saved_pending_delivery")
        self.assertEqual(rows["queued"]["evidence"]["resolutions"][0]["message_id"], queued)
        self.assertEqual(rows["new"]["id"], new)
        self.assertEqual(rows["missing"]["processing"], "insufficient_data")
        self.assertIsNone(rows["missing"]["id"])
        self.assertIsNone(rows["missing"]["body"])

    def test_followup_requires_committed_acceptance_or_successful_receipt(self):
        for status in ("accepted", "queued", "delivered", "failed", "arbitrary", "invalid", "no-token"):
            task = self.task(status)
            self.message(status)
            event = dict(status=status, token="synthetic-token", message="Synthetic instruction")
            if status == "no-token":
                event = dict(status="delivered", message="Synthetic instruction")
            self.message(status, "followup", "not json" if status == "invalid" else json.dumps(event))
            # Followup's answer transport must not disguise a failed/unstructured event as an answer.
            self.message(status, "answer_queued",
                         core.FOLLOWUP_HEAD + "\n\nSynthetic")
            state = task_questions.question_state(self.con, task)
            self.assertEqual(state["processing"], "resolved" if status in ("accepted", "delivered") else
                             "saved_pending_delivery" if status == "queued" else "awaiting_decision")
        self.assertEqual({r["task_id"] for r in self.rows()}, {"queued", "failed", "arbitrary", "invalid", "no-token"})

    def test_answer_in_other_task_or_before_latest_question_is_not_resolution(self):
        self.task("first")
        self.task("other", "acked")
        self.message("first", "answer")
        self.message("first")
        self.message("other", "answer")
        self.assertEqual({r["task_id"] for r in self.rows()}, {"first"})

    def test_queued_followup_consumed_later_is_resolved(self):
        self.task()
        self.message("task")
        fid = self.message("task", "followup", json.dumps(dict(status="queued", token="token", message="instruction")))
        mid = self.message("task", "answer_queued",
                           core.FOLLOWUP_HEAD + "\n\ninstruction", followup_id=fid)
        self.assertEqual(self.rows()[0]["processing"], "saved_pending_delivery")
        self.con.execute("UPDATE messages SET read_at=30 WHERE id=?", (mid,))
        self.assertEqual(self.rows(), [])

    def test_failed_followup_requires_real_queue_or_later_delivery_receipt(self):
        self.task()
        self.message("task")
        fid = self.message("task", "followup", json.dumps(dict(status="failed", token="token", message="instruction")))
        self.assertEqual(self.rows()[0]["processing"], "awaiting_decision")
        mid = self.message("task", "answer_queued",
                           core.FOLLOWUP_HEAD + "\n\ninstruction", followup_id=fid)
        row, = self.rows()
        self.assertEqual(row["processing"], "saved_pending_delivery")
        self.assertIn(mid, [e["message_id"] for e in row["evidence"]["resolutions"]])
        self.con.execute("UPDATE messages SET read_at=30 WHERE id=?", (mid,))
        self.assertEqual(self.rows(), [])

    def test_same_followup_text_without_id_link_never_proves_delivery(self):
        self.task()
        self.message("task")
        self.message("task", "followup", json.dumps(dict(status="queued", token="token", message="instruction")))
        self.message("task", "answer", core.FOLLOWUP_HEAD + "\n\ninstruction")
        self.assertEqual(self.rows()[0]["processing"], "saved_pending_delivery")


if __name__ == "__main__":
    unittest.main()
