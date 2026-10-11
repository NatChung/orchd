"""Synthetic dashboard submission, exact forwarding receipts and lifecycle integration."""
import contextlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from orchd import app_worker, cli, core, entry, mcp_server, snapshot, store
from tests.test_entry import EntryRuntime
from tests.app_support import AppBase


class DashboardAnswersTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "orchd.db"
        self.con = store.connect(self.path)
        self.addCleanup(self.con.close)
        self.rt = EntryRuntime()
        self.orch = core.start_orch(self.con, self.rt, "opus")["id"]
        entry.bind(self.con, self.rt, self.orch)
        entry.touch(self.con, "desktop", "synthetic-desktop")

    def task(self, model="sonnet"):
        return core.dispatch(self.con, self.rt, orch_thread=self.orch, repo="synthetic",
                             title="Synthetic task", instructions="Synthetic work", done_when="Synthetic check",
                             model=model, model_reason="Synthetic choice", task_type="code")["id"]

    def ask(self, **kwargs):
        return entry.ask_nat(self.con, self.rt, self.orch, "Synthetic question", **kwargs)["question_id"]

    def submit(self, q, **kwargs):
        args = dict(expected_version=1, expected_state="current", text="Synthetic answer",
                    idempotency_key="synthetic-key", orch_id=self.orch)
        args.update(kwargs)
        return entry.submit_answer(self.con, self.rt, "desktop", q, **args)

    def processing(self, reply):
        return next(r["processing"] for r in snapshot.read(self.path)["replies"] if r["id"] == reply)

    def test_submit_promotes_wakes_only_orch_and_duplicates_have_no_side_effects(self):
        task = self.task()
        q, following = self.ask(task_id=task), self.ask()
        self.rt.sent.clear()
        result = self.submit(q)
        rid = result["message_id"]
        row = entry._row(self.con, rid)
        self.assertEqual((row["direction"], row["kind"], row["reply_to"], row["task_id"]),
                         ("in", "reply", q, task))
        self.assertEqual(row["body"], "Synthetic answer")
        self.assertEqual((row["delivery"], row["recipient"], row["attempts"]), ("delivered", self.orch, 1))
        self.assertIn(f"entry message {rid}", self.rt.sent[0][2])
        self.assertEqual(self.rt.sent[0][0], self.rt.orch_socket_path(self.orch))
        self.assertEqual(entry._row(self.con, following)["question_state"], "current")
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM messages WHERE kind LIKE 'answer%'").fetchone()[0], 0)
        before = len(self.rt.sent)
        for key in ("synthetic-key", "synthetic-new-key"):
            again = self.submit(q, idempotency_key=key, text="Ignored retry text")
            self.assertEqual(again["message_id"], rid)
            self.assertTrue(again["duplicate"])
        self.assertEqual(len(self.rt.sent), before)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM entry_messages WHERE kind='reply'").fetchone()[0], 1)
        with self.assertRaisesRegex(ValueError, "key belongs"):
            self.submit(following)
        self.assertEqual(self.processing(rid)["worker_forwarded"], None)
        self.assertEqual(self.processing(rid)["orch_notification"], "notified")
        entry.inbox(self.con, self.orch)
        self.assertIsNotNone(self.processing(rid)["orch_read_at"])

    def test_invalid_question_version_state_and_binding_rejected_without_writes(self):
        q, queued = self.ask(), self.ask()
        before = self.con.total_changes
        for question, args in ((99999, {}), (q, {"expected_version": 2}), (queued, {}),
                               (q, {"expected_state": "queued"}), (q, {"orch_id": "wrong-orch"}),
                               (q, {"text": " "}), (q, {"idempotency_key": ""})):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.submit(question, **args)
        self.assertEqual(self.con.total_changes, before)
        other = core.start_orch(self.con, self.rt, "opus")["id"]
        entry.bind(self.con, self.rt, other, force=True)
        with self.assertRaises(ValueError):
            self.submit(q, orch_id=other)

    def test_stale_second_connection_returns_original_receipt(self):
        q = self.ask()
        first = self.submit(q)
        other = store.connect(self.path)
        try:
            second = entry.submit_answer(other, self.rt, "desktop", q, 1, "current", "Synthetic retry",
                                         "other-key", orch_id=self.orch)
            self.assertEqual(second["message_id"], first["message_id"])
            self.assertTrue(second["duplicate"])
        finally:
            other.close()

    def test_concurrent_submissions_create_one_reply_and_one_wake(self):
        q = self.ask()
        self.rt.sent.clear()
        def submit_from_connection(key):
            con = store.connect(self.path)
            try:
                return entry.submit_answer(con, self.rt, "desktop", q, 1, "current", "Synthetic concurrent answer",
                                           key, orch_id=self.orch)
            finally:
                con.close()
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(submit_from_connection, ("concurrent-a", "concurrent-b")))
        self.assertEqual(results[0]["message_id"], results[1]["message_id"])
        self.assertEqual(sum(r["duplicate"] for r in results), 1)
        self.assertEqual(len(self.rt.sent), 1)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM entry_messages WHERE kind='reply'").fetchone()[0], 1)

    def test_revision_and_withdrawn_reply_retries_do_not_reuse_old_answers(self):
        q = self.ask()
        self.submit(q)
        new = self.ask(supersedes=q, expected_version=1, expected_state="answered", reason="Synthetic revision")
        with self.assertRaises(ValueError):
            self.submit(q)
        with self.assertRaises(ValueError):
            self.submit(new, idempotency_key="revision")
        rid = self.submit(new, expected_version=2, idempotency_key="revision")["message_id"]
        self.assertEqual(entry._row(self.con, rid)["reply_to"], new)
        for state in ("snoozed", "cancelled"):
            pending = self.ask()
            entry.question_transition(self.con, self.rt, "desktop", pending, 1, "current", state,
                                      actor="synthetic", revisit_at=0, reason="Synthetic withdrawal")
            with self.assertRaises(ValueError):
                self.submit(pending, idempotency_key=state)

    def test_failed_save_rolls_back_question_history_promotion_and_key(self):
        q, following = self.ask(), self.ask()
        before = entry.question_show(self.con, "desktop", q)
        with patch("orchd.entry._insert", side_effect=sqlite3.OperationalError("Synthetic failure")):
            with self.assertRaises(sqlite3.OperationalError):
                self.submit(q)
        self.assertEqual(entry.question_show(self.con, "desktop", q), before)
        self.assertEqual(entry._row(self.con, following)["question_state"], "queued")
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM entry_answer_submissions").fetchone()[0], 0)

    def test_inbox_read_without_wake_does_not_invent_notification_receipt(self):
        q = self.ask()
        with patch("orchd.entry.deliver_inbound", return_value={}):
            rid = self.submit(q)["message_id"]
        entry.inbox(self.con, self.orch)
        self.assertEqual(entry._row(self.con, rid)["delivery"], "delivered")
        processing = self.processing(rid)
        self.assertIsNotNone(processing["orch_read_at"])
        self.assertIsNone(processing["orch_notification"])
        self.assertIsNone(processing["orch_notified_at"])

    def test_offline_and_failed_wake_preserve_reply_without_retry_wake(self):
        for failure in ("offline", "send"):
            with self.subTest(failure=failure):
                q = self.ask()
                if failure == "offline":
                    store.stop_orch(self.con, self.orch)
                else:
                    self.rt.uds_fails = True
                rid = self.submit(q, idempotency_key=failure)["message_id"]
                self.assertEqual(entry._row(self.con, rid)["delivery"], "pending" if failure == "offline" else "failed")
                attempts = entry._row(self.con, rid)["attempts"]
                self.submit(q, idempotency_key=failure)
                self.assertEqual(entry._row(self.con, rid)["attempts"], attempts)
                self.con.execute("UPDATE orchs SET stopped_at=NULL WHERE id=?", (self.orch,))
                self.rt.uds_fails = False
                entry.deliver_inbound(self.con, self.rt, "desktop")
                self.assertEqual(entry._row(self.con, rid)["delivery"], "delivered")

    def test_mcp_claude_forwarding_persists_exact_link_and_unlinked_answer_stays_null(self):
        task = self.task()
        rid = self.submit(self.ask(task_id=task))["message_id"]
        result = mcp_server.call("answer", {"task_id": task, "entry_reply_id": rid}, self.orch, self.con, self.rt)
        self.assertEqual(result["status"], "delivered")
        message = store.latest_message(self.con, task, "answer")
        self.assertEqual(message["entry_reply_id"], rid)
        self.assertEqual(self.processing(rid)["worker_forwarded"], "transport_accepted")
        core.answer(self.con, self.rt, task, "Synthetic legacy answer")
        self.assertIsNone(store.latest_message(self.con, task, "answer")["entry_reply_id"])
        with self.assertRaises(ValueError):
            core.answer(self.con, self.rt, self.task(), entry_reply_id=rid)

    def test_codex_queue_and_flush_retain_link_across_reconnect(self):
        task = self.task("sol")
        rid = self.submit(self.ask(task_id=task))["message_id"]
        self.rt.alive_pids.add("4242")
        with patch.object(self.rt, "sleep", create=True):
            result = core.answer(self.con, self.rt, task, entry_reply_id=rid)
        self.assertEqual(result["status"], "queued")
        self.assertEqual(self.processing(rid)["worker_forwarded"], "pending")
        self.assertEqual(store.pending_answers(self.con, task)[0]["entry_reply_id"], rid)
        self.rt.alive_pids.clear()
        other = store.connect(self.path)
        try:
            self.assertEqual(core.answer(other, self.rt, task, flush=True)["status"], "delivered")
            self.assertEqual(store.latest_message(other, task, "answer")["entry_reply_id"], rid)
        finally:
            other.close()
        p = self.processing(rid)
        self.assertEqual(p["worker_forwarded"], "transport_accepted")
        self.assertEqual((p["worker_confirmed"], p["worker_executed"]), ("unknown", "unknown"))

    def test_unsnooze_is_tail_and_new_questions_follow_it(self):
        first, second, third = self.ask(), self.ask(), self.ask()
        entry.question_transition(self.con, self.rt, "desktop", first, 1, "current", "snoozed",
                                  actor="synthetic", revisit_at=0, note="Synthetic note")
        data = snapshot.read(self.path)
        snoozed = next(q for q in data["entry_messages"] if q["id"] == first)
        self.assertTrue(snoozed["revisit_due"])
        self.assertEqual(snoozed["note"], "Synthetic note")
        self.assertEqual(snoozed["history"][-1]["to_state"], "snoozed")
        result = entry.question_transition(self.con, self.rt, "desktop", first, 1, "snoozed", "queued",
                                           actor="synthetic")
        self.assertEqual(result["state"], "queued")
        self.assertFalse(result["revisit_due"])
        fourth = self.ask()
        bound = entry.get_entry(self.con, "desktop")
        self.assertEqual([r["id"] for r in entry.queued_questions(self.con, bound)], [third, first, fourth])
        for q in (second, third):
            self.submit(q, idempotency_key=f"synthetic-{q}")
        self.assertEqual(entry.current_question(self.con, bound)["id"], first)
        with self.assertRaises(ValueError):
            entry.question_transition(self.con, self.rt, "desktop", first, 1, "snoozed", "queued", actor="synthetic")

    def test_unsnooze_without_current_becomes_current_and_supersede_count_is_correct(self):
        q = self.ask()
        entry.question_transition(self.con, self.rt, "desktop", q, 1, "current", "snoozed",
                                  actor="synthetic", revisit_at=0)
        result = entry.question_transition(self.con, self.rt, "desktop", q, 1, "snoozed", "queued", actor="synthetic")
        self.assertEqual(result["state"], "current")
        second = self.ask()
        revised = self.ask(supersedes=q, expected_version=1, expected_state="current", reason="Synthetic revision")
        wire = entry._row(self.con, second)["wire_text"]
        self.assertIn("1 more queued", wire)
        self.assertEqual(entry._row(self.con, revised)["version"], 2)

    def test_cli_answer_unsnooze_and_help(self):
        q = self.ask()
        out = io.StringIO()
        with patch.dict(os.environ, ORCHD_HOME=self.tmp.name), patch("orchd.cli.Runtime", return_value=self.rt), \
                contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["entry-question", "answer", str(q), "--expected-version", "1",
                                       "--expected-state", "current", "--text", "Synthetic CLI answer", "--orch-id",
                                       self.orch, "--idempotency-key", "cli", "--json"]), 0)
        self.assertEqual(json.loads(out.getvalue())["reply_to"], q)
        snoozed = self.ask()
        entry.question_transition(self.con, self.rt, "desktop", snoozed, 1, "current", "snoozed",
                                  actor="synthetic", revisit_at=0)
        with patch.dict(os.environ, ORCHD_HOME=self.tmp.name), patch("orchd.cli.Runtime", return_value=self.rt), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["entry-question", "un-snooze", str(snoozed), "--expected-version", "1",
                                       "--expected-state", "snoozed"]), 0)
        self.assertEqual(entry._row(self.con, snoozed)["question_state"], "current")
        for action in ("answer", "un-snooze"):
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as caught:
                cli.build_parser().parse_args(["entry-question", action, "--help"])
            self.assertEqual(caught.exception.code, 0)


if __name__ == "__main__":
    unittest.main()


class AppReplyReceiptTest(AppBase):
    def test_app_receipt_copies_persisted_reply_link(self):
        mid = store.add_message(self.con, self.id, store.QUEUED, "Synthetic app answer", entry_reply_id=123)
        self.idle()
        result = self.supervisor.flush()
        self.assertEqual(result["status"], "delivered")
        self.assertEqual(store.latest_message(self.con, self.id, "answer")["entry_reply_id"], 123)
        self.assertIsNotNone(self.con.execute("SELECT read_at FROM messages WHERE id=?", (mid,)).fetchone()[0])
