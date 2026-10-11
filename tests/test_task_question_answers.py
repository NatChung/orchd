import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from orchd import cli, core, snapshot, store, task_question_answers


class Runtime:
    def __init__(self):
        self.sent = []
        self.resumed = []
        self.busy = False

    def send_uds(self, socket, session, text):
        self.sent.append((socket, session, text))

    def pid_alive(self, pid):
        return self.busy

    def sleep(self, seconds):
        pass

    def codex_log(self, task_id):
        return "/synthetic/log"

    def resume_codex_worker(self, worktree, log, session, text, model):
        self.resumed.append(text)
        return "456"

    def live_jobs(self):
        return {}


class TaskQuestionAnswerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "orchd.db"
        self.con = store.connect(self.path)
        self.addCleanup(self.con.close)
        self.rt = Runtime()
        self.task()
        self.qid = store.add_message(self.con, "task", "question", "Synthetic question")

    def task(self, tid="task", model="gpt-test", status="question"):
        self.con.execute("INSERT INTO tasks(id,repo,repo_path,title,instructions,done_when,orch_thread,codex_bin,"
                         "status,created_at,updated_at,model,worktree,session_id,job_id,socket) "
                         "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (tid, "synthetic", "/synthetic", "Synthetic task", "instruction", "check", "synthetic-orch",
                          "codex", status, 1, 2, model, "/synthetic/wt", "session", "123", "/synthetic/socket"))

    def submit(self, **kw):
        args = dict(task_id="task", question_id=self.qid, text="Synthetic answer", idempotency_key="key")
        args.update(kw)
        return task_question_answers.submit(self.con, self.rt, **args)

    def test_deliver_through_core_answer_and_replay_same_receipt_after_reconnect(self):
        with patch("orchd.core.answer", wraps=core.answer) as answer:
            receipt = self.submit(actor="operator")
        answer.assert_called_once_with(self.con, self.rt, "task", flush=True)
        self.assertEqual(receipt["status"], "delivered")
        self.assertEqual(receipt["pending"], 0)
        self.assertEqual(len(self.rt.resumed), 1)
        self.con.close()
        self.con = store.connect(self.path)
        self.addCleanup(self.con.close)
        self.assertEqual(self.submit(actor="operator"), receipt)
        self.assertEqual(len(self.rt.resumed), 1)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM messages WHERE kind='answer_queued'").fetchone()[0], 1)
        self.assertEqual(snapshot.read(self.path, schema_version=2)["pending_worker_questions"], [])

    def test_queue_busy_codex_replay_then_regular_flush_delivers_once(self):
        self.rt.busy = True
        receipt = self.submit()
        self.assertEqual((receipt["status"], receipt["pending"]), ("queued", 1))
        self.assertEqual(self.submit(), receipt)
        self.assertEqual(self.rt.resumed, [])
        self.assertEqual(snapshot.read(self.path, schema_version=2)["pending_worker_questions"][0]["processing"],
                         "saved_pending_delivery")
        self.rt.busy = False
        self.assertEqual(core.answer(self.con, self.rt, "task", flush=True)["status"], "delivered")
        self.assertEqual(self.submit(), receipt)  # immutable submission receipt, not later transport state
        self.assertEqual(len(self.rt.resumed), 1)

    def test_claude_delivery_and_failed_delivery_are_durably_idempotent(self):
        store.update_task(self.con, "task", model="claude-test")
        receipt = self.submit()
        self.assertEqual((receipt["status"], receipt["pending"]), ("delivered", 0))
        self.assertEqual(self.submit(), receipt)
        self.assertEqual(len(self.rt.sent), 1)
        self.assertIn(f"[orchd answer task]\nSynthetic answer", self.rt.sent[0][2])
        self.task("failed", model="claude-test")
        qid = store.add_message(self.con, "failed", "question", "Synthetic question")
        with patch.object(self.rt, "send_uds", side_effect=OSError("synthetic delivery failure")):
            failed = self.submit(task_id="failed", question_id=qid)
        self.assertEqual((failed["status"], failed["pending"]), ("failed", 1))
        self.assertEqual(self.submit(task_id="failed", question_id=qid), failed)
        self.assertEqual(len(self.rt.sent), 1)

    def test_stale_wrong_task_answered_queued_and_closed_are_rejected_without_writes(self):
        for reason in ("newer", "wrong-task", "answered", "queued", "closed", "missing"):
            with self.subTest(reason=reason):
                tid = reason
                self.task(tid)
                qid = store.add_message(self.con, tid, "question", "Synthetic question")
                if reason == "newer":
                    store.add_message(self.con, tid, "question", "Newer synthetic question")
                elif reason == "wrong-task":
                    qid = self.qid
                elif reason in ("answered", "queued"):
                    store.add_message(self.con, tid, "answer" if reason == "answered" else "answer_queued", "answer")
                elif reason == "closed":
                    store.update_task(self.con, tid, status="closed")
                elif reason == "missing":
                    self.con.execute("DELETE FROM messages WHERE id=?", (qid,))
                before = self.con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
                with self.assertRaisesRegex(ValueError, "closed" if reason == "closed" else "stale"):
                    self.submit(task_id=tid, question_id=qid)
                self.assertEqual(self.con.execute("SELECT COUNT(*) FROM messages").fetchone()[0], before)
        self.assertEqual(self.rt.resumed, [])

    def test_duplicate_key_payload_conflict_and_duplicate_recovery_after_close(self):
        receipt = self.submit()
        for changes in (dict(text="different"), dict(actor="operator"), dict(question_id=self.qid + 1)):
            with self.assertRaisesRegex(ValueError, "idempotency conflict"):
                self.submit(**changes)
        store.update_task(self.con, "task", status="closed")
        self.assertEqual(self.submit(), receipt)
        self.assertEqual(len(self.rt.resumed), 1)

    def test_validation_reads_task_after_acquiring_task_lock(self):
        original = store.task_delivery

        @contextlib.contextmanager
        def closing_lock(con, ids, **kw):
            with original(con, ids, **kw):
                if ids == ["task"]:
                    store.update_task(con, "task", status="closed")
                yield

        with patch("orchd.store.task_delivery", side_effect=closing_lock):
            with self.assertRaisesRegex(ValueError, "closed"):
                self.submit()
        self.assertEqual(store.pending_answer_count(self.con, "task"), 0)

    def test_crash_after_acceptance_recovers_saved_receipt_without_sending_again(self):
        with patch("orchd.core.answer", side_effect=SystemExit("synthetic crash")):
            with self.assertRaises(SystemExit):
                self.submit()
        self.con.close()
        self.con = store.connect(self.path)
        self.addCleanup(self.con.close)
        with patch("orchd.core.answer", side_effect=AssertionError("retry must not send")):
            saved = self.submit()
        self.assertEqual((saved["status"], saved["pending"]), ("queued", 1))
        self.assertEqual(store.pending_answer_count(self.con, "task"), 1)

    def test_concurrent_duplicate_waits_for_published_receipt_and_sends_once(self):
        started, release = threading.Event(), threading.Event()
        results, errors = [], []
        original = self.rt.resume_codex_worker

        def sending(*args):
            started.set()
            if not release.wait(5):
                raise AssertionError("test timed out")
            return original(*args)

        def submit():
            con = store.connect(self.path)
            try:
                results.append(task_question_answers.submit(con, self.rt, "task", self.qid, "Synthetic answer", "key"))
            except Exception as error:
                errors.append(error)
            finally:
                con.close()

        with patch.object(self.rt, "resume_codex_worker", side_effect=sending):
            first = threading.Thread(target=submit)
            second = threading.Thread(target=submit)
            first.start()
            self.assertTrue(started.wait(5))
            second.start()
            release.set()
            first.join(5)
            second.join(5)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0], results[1])
        self.assertEqual(len(self.rt.resumed), 1)

    def test_crash_after_transport_before_receipt_publication_does_not_resend(self):
        original = core.answer

        def send_then_crash(*args, **kw):
            original(*args, **kw)
            raise SystemExit("synthetic crash after transport")

        with patch("orchd.core.answer", side_effect=send_then_crash):
            with self.assertRaises(SystemExit):
                self.submit()
        self.assertEqual(store.pending_answer_count(self.con, "task"), 0)
        self.assertEqual(self.submit()["status"], "queued")  # recovered acceptance receipt, not a second send
        self.assertEqual(len(self.rt.resumed), 1)

    def test_actor_visible_in_orch_inbox_and_list_open_without_changing_mcp_answer(self):
        receipt = self.submit(actor="operator")
        rows = core.inbox(self.con, "synthetic-orch")
        event, = [r for r in rows if r["kind"] == "task_question_answer"]
        self.assertEqual(json.loads(event["body"])["receipt"], receipt)
        task, = core.list_open(self.con, self.rt)
        self.assertEqual(task["latest_question_answer"]["actor"], "operator")
        self.assertEqual(task["latest_question_answer"]["receipt"], receipt)
        self.assertEqual(core.answer(self.con, self.rt, "task", text="ordinary MCP answer")["status"], "delivered")
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM messages WHERE kind='task_question_answer'").fetchone()[0], 1)

    def test_cli_text_file_json_receipts_and_errors(self):
        text = Path(self.temp.name) / "answer.txt"
        text.write_text("Synthetic answer\n第二行", encoding="utf-8")
        args = ["task-question", "answer", "--task", "task", "--question-id", str(self.qid),
                "--text-file", str(text), "--idempotency-key", "cli-key", "--actor", "operator"]
        def run(args):
            out = io.StringIO()
            with patch.dict(os.environ, ORCHD_HOME=self.temp.name), patch("orchd.cli.Runtime", return_value=self.rt), \
                    contextlib.redirect_stdout(out):
                code = cli.main(args)
            return code, json.loads(out.getvalue())
        code, receipt = run(args)
        self.assertEqual(code, 0)
        self.assertEqual(receipt["status"], "delivered")
        self.assertIn("第二行", self.rt.resumed[0])
        self.assertEqual(run(args), (code, receipt))
        args[args.index("cli-key")] = "new-key"
        code, receipt = run(args)
        self.assertEqual(code, 1)
        self.assertEqual((receipt["status"], receipt["pending"]), ("failed", 0))
        self.assertIn("stale", receipt["error"])
        text.unlink()
        self.rt.busy = True
        new_question = store.add_message(self.con, "task", "question", "New question")
        self.submit(question_id=new_question, idempotency_key="queued-key")
        failed = run(args)[1]
        self.assertEqual((failed["status"], failed["pending"]), ("failed", 1))

    def dismiss(self, **kw):
        args = dict(task_id="task", question_id=self.qid, reason="Decision expired", idempotency_key="dismiss-key")
        args.update(kw)
        return task_question_answers.dismiss(self.con, **args)

    def test_dismiss_removes_pending_preserves_audit_and_notifies_orch_without_worker_send(self):
        before_schema = self.con.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall()
        receipt = self.dismiss(actor="operator")
        data = snapshot.read(self.path, schema_version=2)
        self.assertEqual(data["pending_worker_questions"], [])
        audit, = data["worker_question_resolutions"]
        self.assertEqual((audit["question_id"], audit["actor"], audit["reason"]),
                         (self.qid, "operator", "Decision expired"))
        self.assertGreater(audit["created_at"], 0)
        self.assertEqual(receipt["status"], "dismissed")
        self.assertEqual(store.get_task(self.con, "task")["status"], "question")
        self.assertEqual((self.rt.sent, self.rt.resumed, store.pending_answer_count(self.con, "task")), ([], [], 0))
        event, = [r for r in core.inbox(self.con, "synthetic-orch") if r["kind"] == "question_dismissed"]
        self.assertEqual(json.loads(event["body"])["receipt"], receipt)
        self.assertEqual(self.con.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall(), before_schema)
        self.assertIsNone(self.con.execute("SELECT 1 FROM sqlite_master WHERE name='task_question_answers'").fetchone())
        self.assertNotIn("worker_question_resolutions", snapshot.read(self.path, schema_version=1))
        with self.assertRaisesRegex(ValueError, "stale"):
            self.submit()

    def test_dismiss_stale_closed_and_already_answered_rejected(self):
        for status in ("stale", "closed", "answered", "queued", "wrong-task"):
            with self.subTest(status=status):
                self.task(status)
                qid = store.add_message(self.con, status, "question", "question")
                if status == "closed":
                    store.update_task(self.con, status, status="closed")
                elif status == "stale":
                    store.add_message(self.con, status, "question", "newer")
                elif status in ("answered", "queued"):
                    store.add_message(self.con, status, "answer" if status == "answered" else "answer_queued", "answer")
                else:
                    qid = self.qid
                with self.assertRaisesRegex(ValueError, "closed" if status == "closed" else "stale"):
                    self.dismiss(task_id=status, question_id=qid)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM messages WHERE kind='question_dismissed'").fetchone()[0], 0)

    def test_dismiss_replay_after_reconnect_and_new_question_key_conflicts(self):
        receipt = self.dismiss()
        self.con.close()
        self.con = store.connect(self.path)
        self.addCleanup(self.con.close)
        new = store.add_message(self.con, "task", "question", "newer")
        self.assertEqual(self.dismiss(), receipt)
        self.assertEqual(snapshot.read(self.path, schema_version=2)["pending_worker_questions"][0]["id"], new)
        for changes in (dict(reason="other"), dict(actor="operator"), dict(question_id=new)):
            with self.assertRaisesRegex(ValueError, "idempotency conflict"):
                self.dismiss(**changes)
        with self.assertRaisesRegex(ValueError, "idempotency conflict"):
            self.submit(idempotency_key="dismiss-key")
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM messages WHERE kind='question_dismissed'").fetchone()[0], 1)

    def test_dismiss_rechecks_closed_under_lock(self):
        original = store.task_delivery
        @contextlib.contextmanager
        def closing_lock(con, ids, **kw):
            with original(con, ids, **kw):
                if ids == ["task"]:
                    store.update_task(con, "task", status="closed")
                yield
        with patch("orchd.store.task_delivery", side_effect=closing_lock):
            with self.assertRaisesRegex(ValueError, "closed"):
                self.dismiss()

    def test_dismiss_cli_json_and_duplicate_no_runtime_created(self):
        args = ["task-question", "dismiss", "--task", "task", "--question-id", str(self.qid),
                "--reason", "Expired decision", "--idempotency-key", "dismiss-cli", "--actor", "operator"]
        outputs = []
        for _ in range(2):
            out = io.StringIO()
            with patch.dict(os.environ, ORCHD_HOME=self.temp.name), \
                    patch("orchd.cli.Runtime", side_effect=AssertionError("dismiss must not create runtime")), \
                    contextlib.redirect_stdout(out):
                self.assertEqual(cli.main(args), 0)
            outputs.append(json.loads(out.getvalue()))
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(outputs[0]["status"], "dismissed")

    def test_followup_failed_resume_queue_and_later_delivery_use_exact_event_id(self):
        with patch.object(self.rt, "resume_codex_worker", side_effect=OSError("synthetic resume failure")):
            result = core.followup(self.con, self.rt, "task", "same text")
        self.assertEqual(result["status"], "failed")
        fid = self.con.execute("SELECT id FROM messages WHERE kind='followup'").fetchone()[0]
        queued, = store.pending_answers(self.con, "task")
        self.assertEqual(json.loads(queued["evidence"])["followup_id"], fid)
        self.assertEqual(snapshot.read(self.path, schema_version=2)["pending_worker_questions"][0]["processing"],
                         "saved_pending_delivery")
        core.answer(self.con, self.rt, "task", flush=True)
        delivered = self.con.execute("SELECT evidence FROM messages WHERE kind='answer'").fetchone()[0]
        self.assertEqual(json.loads(delivered)["followup_id"], fid)
        self.assertEqual(snapshot.read(self.path, schema_version=2)["pending_worker_questions"], [])

    def test_claude_followup_delivery_receipt_links_event_id(self):
        store.update_task(self.con, "task", model="claude-test")
        self.assertEqual(core.followup(self.con, self.rt, "task", "same text")["status"], "delivered")
        fid = self.con.execute("SELECT id FROM messages WHERE kind='followup'").fetchone()[0]
        delivered = self.con.execute("SELECT evidence FROM messages WHERE kind='answer'").fetchone()[0]
        self.assertEqual(json.loads(delivered)["followup_id"], fid)


if __name__ == "__main__":
    unittest.main()
