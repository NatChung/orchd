"""Question lifecycle: isolated databases and synthetic bodies only."""
import contextlib
import io
import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from orchd import cli, core, entry, mcp_server, store
from tests.test_entry import EntryRuntime


class LifecycleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = store.connect(Path(self.tmp.name) / "orchd.db")
        self.rt = EntryRuntime()
        self.orch = core.start_orch(self.con, self.rt, "opus")["id"]
        entry.bind(self.con, self.rt, self.orch)
        entry.touch(self.con, "desktop", "desk-1")

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def ask(self, text="Synthetic question", **kwargs):
        return entry.ask_nat(self.con, self.rt, self.orch, text, **kwargs)["question_id"]

    def show(self, q):
        return entry.question_show(self.con, "desktop", q)

    def transition(self, q, state, version=1, expected="current", **kwargs):
        return entry.question_transition(self.con, self.rt, "desktop", q, version, expected, state,
                                         actor="operator:synthetic", **kwargs)

    def supersede(self, q, version=1, state="current"):
        return self.ask("Revised synthetic question", supersedes=q, expected_version=version,
                        expected_state=state, reason="clarified wording")

    def test_successor_cancels_old_question_with_audit_and_cas(self):
        old_orch = self.orch
        q = self.ask()
        next_q = self.ask()
        self.orch = core.start_orch(self.con, self.rt, "opus")["id"]
        entry.bind(self.con, self.rt, self.orch, force=True)
        entry.question_transition(self.con, self.rt, "desktop", q, 1, "current", "cancelled",
                                  actor=f"orch:{self.orch}", reason="obsolete after handover")
        shown = self.show(q)
        self.assertEqual(shown["state"], "cancelled")
        audit = shown["history"][-1]
        self.assertEqual(audit["actor"], f"orch:{self.orch}")
        self.assertEqual(audit["reason"], "obsolete after handover")
        self.assertEqual(audit["original_orch_id"], old_orch)
        self.assertEqual(self.show(next_q)["state"], "queued")
        before = self.show(q)
        with self.assertRaisesRegex(ValueError, "mismatch"):
            entry.question_transition(self.con, self.rt, "desktop", q, 2, "cancelled", "snoozed",
                                      actor=f"orch:{self.orch}", reason="stale", revisit_at=0)
        self.assertEqual(self.show(q), before)

    def task(self):
        return core.dispatch(self.con, self.rt, orch_thread=self.orch, repo="demo", title="Synthetic task",
                             instructions="synthetic", done_when="synthetic", model="sonnet",
                             model_reason="test", task_type="outward")["id"]

    def test_close_cancels_pending_questions_atomically_and_promotes_next(self):
        task = self.task()
        answered = self.ask(task_id=task)
        self.rt.nat_says("desk-1", "Synthetic answer")
        entry.relay(self.con, self.rt, "desktop", "desk-1", reply_to=answered)
        snoozed = self.ask(task_id=task)
        self.transition(snoozed, "snoozed", revisit_at=0)
        current, queued = self.ask(task_id=task), self.ask(task_id=task)
        following = self.ask()
        before = {q: self.show(q) for q in (answered, snoozed)}
        wakes = len(self.rt.woken)
        core.close(self.con, self.rt, task)
        for q in (current, queued):
            shown = self.show(q)
            self.assertEqual(shown["state"], "cancelled")
            self.assertEqual(shown["reason"], "task closed")
            self.assertEqual(shown["history"][-1]["reason"], "task closed")
            self.assertEqual(shown["history"][-1]["original_orch_id"], self.orch)
            self.assertEqual(shown["history"][-1]["actor"], "orchd")
        self.assertEqual(self.show(following)["state"], "current")
        self.assertEqual(len(self.rt.woken), wakes + 1)
        self.assertIn(f"[orchd question {following}]", self.rt.woken[-1][2])
        for q, shown in before.items():
            self.assertEqual(self.show(q), shown)
        histories = {q: self.show(q) for q in (current, queued, following)}
        core.close(self.con, self.rt, task)
        self.assertEqual(histories, {q: self.show(q) for q in histories})

    def test_successor_cleans_queued_closed_task_and_missing_owner(self):
        task = self.task()
        first = self.ask()
        cancel, snooze, supersede = [self.ask(task_id=task) for _ in range(3)]
        # Legacy data: old close left questions queued; the old registry entry is gone.
        store.update_task(self.con, task, status="closed")
        original = self.orch
        self.orch = core.start_orch(self.con, self.rt, "opus")["id"]
        entry.bind(self.con, self.rt, self.orch, force=True)
        self.con.execute("DELETE FROM orchs WHERE id=?", (original,))
        current = self.ask()
        for q, state in ((cancel, "cancelled"), (snooze, "snoozed")):
            entry.question_transition(self.con, self.rt, "desktop", q, 1, "queued", state,
                                      actor=f"orch:{self.orch}", orch_id=self.orch,
                                      reason="cleanup", revisit_at=0, note="cleanup")
        revised = self.supersede(supersede, state="queued")
        for q, state in ((cancel, "cancelled"), (snooze, "snoozed"), (supersede, "superseded")):
            shown = self.show(q)
            self.assertEqual(shown["state"], state)
            self.assertEqual(shown["history"][-1]["original_orch_id"], original)
            self.assertEqual(shown["history"][-1]["actor"], f"orch:{self.orch}")
            self.assertTrue(shown["history"][-1]["reason"])
        self.assertEqual(self.show(current)["state"], "current")
        self.assertEqual(self.show(first)["state"], "current")  # old queue stays isolated
        self.assertEqual(self.show(revised)["state"], "queued")
        before = self.show(supersede)
        with self.assertRaisesRegex(ValueError, "mismatch"):
            self.supersede(supersede, version=2, state="superseded")
        self.assertEqual(self.show(supersede), before)
        with self.assertRaises(ValueError):
            entry.submit_answer(self.con, self.rt, "desktop", first, 1, "current", "answer", "key",
                                orch_id=self.orch)

    def test_cross_entry_cleanup_is_rejected_without_writes(self):
        q = self.ask()
        other = core.start_orch(self.con, self.rt, "opus")["id"]
        entry.bind(self.con, self.rt, other, entry_id="other")
        before = self.show(q)
        for state in ("cancelled", "snoozed"):
            with self.assertRaises(ValueError):
                entry.question_transition(self.con, self.rt, "other", q, 1, "current", state,
                                          actor=f"orch:{other}", orch_id=other, reason="cleanup", revisit_at=0)
        with self.assertRaises(ValueError):
            entry.ask_nat(self.con, self.rt, other, "revision", supersedes=q, expected_version=1,
                          expected_state="current", reason="cleanup")
        self.assertEqual(self.show(q), before)

    def test_close_cleans_all_owners_but_only_promotes_bound_queue(self):
        task = self.task()
        old_current, old_queued = self.ask(task_id=task), self.ask(task_id=task)
        untouched = self.ask()
        self.orch = core.start_orch(self.con, self.rt, "opus")["id"]
        entry.bind(self.con, self.rt, self.orch, force=True)
        current = self.ask()
        queued = self.ask()
        # Ownership of a task can change while its earlier questions remain.
        store.update_task(self.con, task, orch_thread=self.orch)
        new_question = self.ask(task_id=task)
        core.close(self.con, self.rt, task)
        for q in (old_current, old_queued, new_question):
            self.assertEqual(self.show(q)["state"], "cancelled")
        self.assertEqual(self.show(untouched)["state"], "queued")
        self.assertEqual(self.show(current)["state"], "current")
        self.assertEqual(self.show(queued)["state"], "queued")

    def test_close_question_failure_rolls_back_task_history_and_promotion(self):
        task = self.task()
        current = self.ask(task_id=task)
        following = self.ask()
        before = {q: self.show(q) for q in (current, following)}
        with patch("orchd.entry._history", side_effect=sqlite3.OperationalError("synthetic audit failure")):
            with self.assertRaises(sqlite3.OperationalError):
                core.close(self.con, self.rt, task)
        self.assertNotEqual(store.get_task(self.con, task)["status"], "closed")
        self.assertEqual(before, {q: self.show(q) for q in before})
        self.assertIsNone(store.latest_message(self.con, task, "close"))
        core.close(self.con, self.rt, task)
        self.assertEqual(self.show(following)["state"], "current")

    def test_snooze_cancel_and_supersede_promote_fifo_and_deliver(self):
        for action in ("snoozed", "cancelled", "superseded"):
            with self.subTest(action=action):
                # Finish each synthetic group before starting another.
                first, second, third = self.ask(), self.ask(), self.ask()
                before = len(self.rt.woken)
                if action == "superseded":
                    new = self.supersede(first)
                    self.assertEqual(self.show(new)["state"], "queued")
                    self.assertEqual(self.show(new)["version"], 2)
                else:
                    self.transition(first, action, **({"revisit_at": time.time() + 60, "note": "later"}
                                                     if action == "snoozed" else {"reason": "no longer needed"}))
                self.assertEqual(self.show(first)["state"], action)
                self.assertEqual(self.show(second)["state"], "current")
                self.assertEqual(self.show(third)["state"], "queued")
                self.assertEqual(len(self.rt.woken), before + 1)
                self.assertIn(f"[orchd question {second}]", self.rt.woken[-1][2])
                for row in list(entry.queued_questions(self.con, entry.get_entry(self.con, "desktop"))):
                    self.transition(row["id"], "cancelled", row["version"], "queued", reason="fixture end")
                self.transition(second, "cancelled", reason="fixture end")

    def test_snooze_due_is_only_metadata(self):
        q = self.ask()
        self.transition(q, "snoozed", revisit_at=time.time() - 60)
        before = len(self.show(q)["history"])
        self.assertTrue(self.show(q)["revisit_due"])
        entry.status(self.con, self.rt, "desktop", "desk-1")
        self.assertIsNone(entry.current_question(self.con, entry.get_entry(self.con, "desktop")))
        self.assertEqual(self.show(q)["state"], "snoozed")
        self.assertEqual(len(self.show(q)["history"]), before)

    def test_compare_version_and_state_rejects_without_partial_writes(self):
        first, second = self.ask(), self.ask()
        for version, state in ((2, "current"), (1, "queued"), (None, "current"), (1, None)):
            for action in ("cancelled", "snoozed", "superseded"):
                with self.subTest(version=version, state=state, action=action):
                    before = [dict(r) for r in self.con.execute("SELECT * FROM entry_messages")]
                    history = [dict(r) for r in self.con.execute("SELECT * FROM entry_question_history")]
                    with self.assertRaises(ValueError):
                        if action == "superseded":
                            self.supersede(first, version, state)
                        else:
                            self.transition(first, action, version, state, reason="synthetic", revisit_at=0)
                    self.assertEqual(before, [dict(r) for r in self.con.execute("SELECT * FROM entry_messages")])
                    self.assertEqual(history, [dict(r) for r in self.con.execute("SELECT * FROM entry_question_history")])
        self.assertEqual(self.show(second)["state"], "queued")

    def test_old_replies_refused_and_revisions_linked(self):
        old = self.ask()
        new = self.supersede(old)
        newest = self.supersede(new, 2)
        self.assertEqual(self.show(old)["superseded_by"], new)
        self.assertEqual(self.show(new)["supersedes"], old)
        self.assertEqual(self.show(new)["superseded_by"], newest)
        self.assertEqual(self.show(newest)["version"], 3)
        self.rt.nat_says("desk-1", "Synthetic answer")
        for q in (old, new):
            with self.assertRaisesRegex(ValueError, "superseded"):
                entry.relay(self.con, self.rt, "desktop", "desk-1", reply_to=q)
        reply = entry.relay(self.con, self.rt, "desktop", "desk-1", reply_to=newest)
        self.assertEqual(reply["reply_to"], newest)
        self.assertEqual(self.show(newest)["state"], "answered")

    def test_answered_revision_does_not_reuse_answer(self):
        task = core.dispatch(self.con, self.rt, orch_thread=self.orch, repo="demo", title="Synthetic task",
                             instructions="synthetic", done_when="synthetic", model="sonnet",
                             model_reason="test", task_type="outward")["id"]
        old = self.ask(task_id=task)
        self.rt.nat_says("desk-1", "Synthetic approval")
        reply = entry.relay(self.con, self.rt, "desktop", "desk-1", reply_to=old)
        self.assertEqual(entry.reply_text(self.con, self.orch, reply["message_id"], task), "Synthetic approval")
        new = self.supersede(old, state="answered")
        self.assertEqual(self.show(new)["state"], "current")
        with self.assertRaisesRegex(ValueError, "superseded"):
            entry.reply_text(self.con, self.orch, reply["message_id"], task)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM entry_messages WHERE reply_to=?", (new,)).fetchone()[0], 0)

    def test_history_records_creation_promotion_answer_and_actor_reason(self):
        first, second = self.ask(), self.ask()
        self.transition(first, "cancelled", reason="synthetic cancellation")
        history = self.show(first)["history"]
        self.assertEqual([(r["from_state"], r["to_state"]) for r in history], [(None, "current"), ("current", "cancelled")])
        self.assertEqual(history[-1]["actor"], "operator:synthetic")
        self.assertEqual(history[-1]["reason"], "synthetic cancellation")
        self.assertGreater(history[-1]["created_at"], 0)
        self.rt.nat_says("desk-1", "Synthetic answer")
        entry.relay(self.con, self.rt, "desktop", "desk-1", reply_to=second)
        history = self.show(second)["history"]
        self.assertEqual([r["to_state"] for r in history], ["queued", "current", "answered"])
        self.assertEqual(history[-1]["actor"], "operator:desk-1")
        self.assertTrue(all(r["version"] == 1 for r in history))

    def test_terminal_pending_questions_never_retry_or_reappear_in_status(self):
        entry.touch(self.con, "desktop", "desk-1")
        self.rt.wake_fails = True
        q = self.ask()
        self.assertEqual(self.show(q)["delivery"], "failed")
        self.transition(q, "cancelled", reason="synthetic")
        self.rt.wake_fails = False
        result = entry.status(self.con, self.rt, "desktop", "desk-1")
        self.assertFalse(any(r["message_id"] == q for r in result["from_orch"]))
        self.assertEqual(entry.deliver_outbound(self.con, self.rt, "desktop"), {})

    def test_queued_transition_does_not_displace_current(self):
        first, second = self.ask(), self.ask()
        self.transition(second, "snoozed", expected="queued", revisit_at=0)
        self.assertEqual(self.show(first)["state"], "current")
        new = self.supersede(second, state="snoozed")
        self.assertEqual(self.show(new)["state"], "queued")
        self.assertEqual(self.show(first)["state"], "current")

    def test_invalid_metadata_and_binding_refused(self):
        q = self.ask()
        for revisit in (None, "tomorrow", float("nan"), float("inf"), -1, True):
            with self.assertRaises(ValueError):
                self.transition(q, "snoozed", revisit_at=revisit)
        with self.assertRaises(ValueError):
            self.transition(q, "cancelled", reason=" ")
        with self.assertRaises(ValueError):
            self.ask(supersedes=q, expected_version=1, expected_state="current")
        other = core.start_orch(self.con, self.rt, "opus")["id"]
        entry.bind(self.con, self.rt, other, force=True)
        with self.assertRaisesRegex(ValueError, "another Orch"):
            entry.question_transition(self.con, self.rt, "desktop", q, 1, "current", "cancelled",
                                      actor=f"orch:{self.orch}", orch_id=self.orch, reason="synthetic")
        self.assertEqual(self.show(q)["state"], "current")

    def test_supersede_failure_rolls_back_history_and_promotion(self):
        first, second = self.ask(), self.ask()
        before = self.show(first)
        with patch("orchd.entry._insert", side_effect=sqlite3.OperationalError("synthetic insert failure")):
            with self.assertRaises(sqlite3.OperationalError):
                self.supersede(first)
        self.assertEqual(self.show(first), before)
        self.assertEqual(self.show(second)["state"], "queued")

    def test_stale_operation_from_another_connection_refused(self):
        q = self.ask()
        other = store.connect(Path(self.tmp.name) / "orchd.db")
        try:
            entry.question_transition(other, self.rt, "desktop", q, 1, "current", "cancelled",
                                      actor="operator:other", reason="synthetic")
            with self.assertRaisesRegex(ValueError, "mismatch"):
                self.transition(q, "snoozed", revisit_at=0)
            self.assertEqual([r["to_state"] for r in self.show(q)["history"]], ["current", "cancelled"])
        finally:
            other.close()

    def test_mcp_routes_with_bound_actor_and_requires_expectations(self):
        q = self.ask()
        meta = {"_meta": {"threadId": self.orch}}
        def call(name, arguments):
            return mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                      "params": dict(meta, name=name, arguments=arguments)}, self.con, self.rt)
        missing = call("entry_question_cancel", {"question_id": q, "reason": "synthetic"})
        self.assertTrue(missing["result"]["isError"])
        good = call("entry_question_snooze", {"question_id": q, "expected_version": 1,
                                              "expected_state": "current", "revisit_at": 0})
        self.assertNotIn("isError", good["result"])
        self.assertEqual(self.show(q)["history"][-1]["actor"], f"orch:{self.orch}")
        revised = call("ask_nat", {"text": "Synthetic revision", "supersedes": q, "expected_version": 1,
                                   "expected_state": "snoozed", "reason": "synthetic"})
        self.assertNotIn("isError", revised["result"])
        self.assertEqual(self.show(q)["state"], "superseded")

    def test_entry_role_cannot_call_lifecycle_orch_tools(self):
        q = self.ask()
        for name in ("entry_question_show", "entry_question_snooze", "entry_question_cancel", "ask_nat"):
            result = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                        "params": {"name": name, "arguments": {"question_id": q},
                                                   "_meta": {"threadId": "desk-1"}}},
                                       self.con, self.rt, role="entry")
            self.assertTrue(result["result"]["isError"])
        self.assertEqual(self.show(q)["state"], "current")

    def test_cli_operations_and_help(self):
        q = self.ask()
        def run(*args):
            out, err = io.StringIO(), io.StringIO()
            with patch.dict(os.environ, ORCHD_HOME=self.tmp.name), patch("orchd.cli.Runtime", return_value=self.rt), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main(["entry-question", *args])
            return code, out.getvalue(), err.getvalue()
        code, out, _ = run("snooze", str(q), "--expected-version", "1", "--expected-state", "current", "--revisit-at", "0")
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["revisit_due"])
        self.assertEqual(run("cancel", str(q), "--expected-version", "2", "--expected-state", "snoozed", "--reason", "synthetic")[0], 1)
        code, out, _ = run("supersede", str(q), "--expected-version", "1", "--expected-state", "snoozed", "--reason", "synthetic", "--text", "Synthetic revision")
        self.assertEqual(code, 0)
        new = json.loads(out)["question_id"]
        self.assertEqual(run("cancel", str(new), "--expected-version", "2", "--expected-state", "current", "--reason", "synthetic")[0], 0)
        self.assertEqual(json.loads(run("show", str(q))[1])["state"], "superseded")
        for action in ("show", "snooze", "cancel", "supersede"):
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as result:
                cli.build_parser().parse_args(["entry-question", action, "--help"])
            self.assertEqual(result.exception.code, 0)


class AdditiveMigrationTest(unittest.TestCase):
    def test_pre_audit_owner_schema_migrates_without_rewriting_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "legacy.db"
            old = sqlite3.connect(path)
            old.executescript(store.SCHEMA.replace("    original_orch_id TEXT,\n", ""))
            old.execute("INSERT INTO entry_question_history(question_id,version,to_state,actor,reason,created_at) "
                        "VALUES(1,1,'current','orch:old','ask_nat',123)")
            old.commit()
            old.close()
            for _ in range(2):
                con = store.connect(path)
                row = dict(con.execute("SELECT * FROM entry_question_history").fetchone())
                self.assertIsNone(row.pop("original_orch_id"))
                self.assertEqual(row, dict(id=1, question_id=1, version=1, from_state=None,
                                          to_state="current", actor="orch:old", reason="ask_nat", created_at=123))
                con.close()

    def test_old_rows_and_columns_preserved_and_migration_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.db"
            old = sqlite3.connect(path)
            # SCHEMA's entry_messages definition is intentionally the pre-lifecycle shape.
            old.executescript(store.SCHEMA)
            old.execute("DROP TABLE entry_question_history")
            old.execute("DROP TABLE entry_answer_submissions")
            old.execute("INSERT INTO messages(task_id,kind,body,created_at) "
                        "VALUES('synthetic-task','answer','Synthetic legacy answer',122)")
            message_columns = [r[1] for r in old.execute("PRAGMA table_info(messages)")]
            old_messages = old.execute("SELECT * FROM messages").fetchall()
            old.execute("INSERT INTO entries(id,orch_id,bound_at) VALUES('synthetic','orch-test',123)")
            old.execute("INSERT INTO entry_messages(entry_id,orch_id,direction,kind,body,body_bytes,body_sha256,"
                        "question_state,delivery,created_at) VALUES('synthetic','orch-test','out','question',"
                        "'Synthetic old question',22,'synthetic-hash','current','pending',123)")
            for state, delivery in (("queued", "held"), ("answered", "delivered")):
                old.execute("INSERT INTO entry_messages(entry_id,orch_id,direction,kind,body,body_bytes,body_sha256,"
                            "question_state,delivery,created_at) VALUES('synthetic','orch-test','out','question',"
                            "'Synthetic historical question',29,'synthetic-hash',?,?,124)", (state, delivery))
            old.execute("INSERT INTO entry_messages(entry_id,orch_id,direction,kind,body,body_bytes,body_sha256,"
                        "reply_to,delivery,created_at) VALUES('synthetic','orch-test','in','reply',"
                        "'Synthetic historical answer',27,'synthetic-hash',3,'delivered',125)")
            old.commit()
            columns = [r[1] for r in old.execute("PRAGMA table_info(entry_messages)")]
            before = old.execute("SELECT * FROM entry_messages").fetchall()
            entries = old.execute("SELECT * FROM entries").fetchall()
            old.close()
            for _ in range(2):
                migrated = store.connect(path)
                self.assertEqual([tuple(r) for r in migrated.execute(f"SELECT {','.join(columns)} FROM entry_messages")], before)
                self.assertEqual(tuple(migrated.execute("SELECT * FROM entries").fetchone()), entries[0])
                row = migrated.execute("SELECT * FROM entry_messages").fetchone()
                self.assertEqual(row["version"], 1)
                self.assertTrue(all(row[key] is None for key in ("supersedes", "superseded_by", "revisit_at", "note", "reason", "queue_order", "notified_at")))
                self.assertEqual(migrated.execute("SELECT COUNT(*) FROM entry_question_history").fetchone()[0], 0)
                self.assertEqual(migrated.execute("SELECT COUNT(*) FROM entry_answer_submissions").fetchone()[0], 0)
                self.assertIn("entry_reply_id", [r["name"] for r in migrated.execute("PRAGMA table_info(messages)")])
                self.assertEqual([tuple(r) for r in migrated.execute(f"SELECT {','.join(message_columns)} FROM messages")],
                                 old_messages)
                self.assertIsNone(migrated.execute("SELECT entry_reply_id FROM messages").fetchone()[0])
                migrated.close()


if __name__ == "__main__":
    unittest.main()
