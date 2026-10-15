"""Issue #60: open entry questions follow the entry to its new owner (rebind, adopt, one-time backfill)."""
import contextlib
import io
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchd import cli, core, entry, store
from tests.test_entry import EntryRuntime


class HandoverTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "orchd.db"
        self.con = store.connect(self.db)
        self.rt = EntryRuntime()
        self.old = core.start_orch(self.con, self.rt, "opus")["id"]
        entry.bind(self.con, self.rt, self.old)
        entry.touch(self.con, "desktop", "desk-1")

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def ask(self, orch, text="Synthetic question"):
        return entry.ask_nat(self.con, self.rt, orch, text)["question_id"]

    def new_orch(self):
        return core.start_orch(self.con, self.rt, "opus")["id"]

    def row(self, q):
        return dict(self.con.execute("SELECT * FROM entry_messages WHERE id=?", (q,)).fetchone())

    def state(self, q):
        return self.row(q)["question_state"]

    def currents(self):
        return [r[0] for r in self.con.execute(
            "SELECT id FROM entry_messages WHERE kind='question' AND question_state='current' ORDER BY id")]

    def test_rebind_moves_current_queued_snoozed_to_new_owner(self):
        current, queued, snoozed = self.ask(self.old), self.ask(self.old), self.ask(self.old)
        entry.question_transition(self.con, self.rt, "desktop", snoozed, 1, "queued", "snoozed",
                                  actor="operator:t", revisit_at=0)
        new = self.new_orch()
        entry.bind(self.con, self.rt, new, force=True)
        for q in (current, queued, snoozed):
            self.assertEqual(self.row(q)["orch_id"], new)
        self.assertEqual([self.state(q) for q in (current, queued, snoozed)], ["current", "queued", "snoozed"])
        view = entry.status(self.con, self.rt, "desktop", "desk-1")
        self.assertEqual(view["current_question"]["message_id"], current)
        self.assertEqual(view["queued_questions"], 1)
        moved = self.con.execute("SELECT * FROM entry_question_history WHERE question_id=? ORDER BY id DESC",
                                 (current,)).fetchone()
        self.assertEqual(moved["original_orch_id"], self.old)
        self.assertEqual(moved["actor"], "orchd")
        self.assertIn(new, moved["reason"])

    def test_operator_reply_to_moved_question_reaches_new_owner(self):
        current = self.ask(self.old)
        new = self.new_orch()
        entry.bind(self.con, self.rt, new, force=True)
        self.rt.nat_says("desk-1", "my answer")
        entry.relay(self.con, self.rt, "desktop", "desk-1", reply_to=current)
        self.assertEqual(self.state(current), "answered")

    def test_two_currents_never_coexist_incoming_current_is_demoted(self):
        old_current = self.ask(self.old)
        new = self.new_orch()
        # Legacy rebind (before #60): owner switched without moving rows, then the new owner asked its own.
        self.con.execute("UPDATE entries SET orch_id=? WHERE id='desktop'", (new,))
        own = self.ask(new)
        self.assertEqual(self.currents(), [old_current, own])  # fixture: stale duplicate current
        entry.reconcile_all(self.con, apply=True, backup_dir=Path(self.tmp.name) / "b")
        self.assertEqual(self.currents(), [own])
        self.assertEqual(self.state(old_current), "queued")
        self.assertEqual(self.row(old_current)["delivery"], "held")
        self.assertEqual(self.row(old_current)["orch_id"], new)
        self.assertEqual(self.row(old_current)["original_orch_id"], self.old)

    def test_next_queued_promoted_when_new_owner_has_no_current(self):
        entry_row = entry.get_entry(self.con, "desktop")
        first, second = self.ask(self.old), self.ask(self.old)
        self.con.execute("UPDATE entry_messages SET question_state='cancelled' WHERE id=?", (first,))
        new = self.new_orch()
        entry.bind(self.con, self.rt, new, force=True)
        self.assertEqual(self.currents(), [second])
        self.assertIsNotNone(entry_row)

    def test_adopt_sweeps_questions_left_on_dead_owner_of_bound_entry(self):
        ghost = self.new_orch()
        q = self.ask(self.old)
        self.con.execute("UPDATE entry_messages SET orch_id=? WHERE id=?", (ghost, q))  # legacy stuck row
        core.stop_orch(self.con, self.rt, ghost)
        task = core.dispatch(self.con, self.rt, orch_thread=ghost, repo="demo", title="T", instructions="i",
                             done_when="d", model="sonnet", model_reason="r", task_type="outward")["id"]
        core.adopt(self.con, self.rt, self.old, [task], from_orch=ghost, force=True)
        self.assertEqual(self.row(q)["orch_id"], self.old)
        self.assertEqual(self.state(q), "current")

    def stuck_on_ghost(self):
        """A dead owner holding one open task and one open entry question of the bound entry."""
        ghost = self.new_orch()
        q = self.ask(self.old)
        self.con.execute("UPDATE entry_messages SET orch_id=? WHERE id=?", (ghost, q))  # legacy stuck row
        core.stop_orch(self.con, self.rt, ghost)
        task = core.dispatch(self.con, self.rt, orch_thread=ghost, repo="demo", title="T", instructions="i",
                             done_when="d", model="sonnet", model_reason="r", task_type="outward")["id"]
        return ghost, task, q

    def test_adopt_failure_moving_questions_rolls_back_task_and_surfaces_error(self):
        ghost, task, q = self.stuck_on_ghost()
        boom = sqlite3.OperationalError("database is locked")
        with mock.patch.object(entry, "sweep_questions", side_effect=boom):
            with self.assertRaises(sqlite3.OperationalError):
                core.adopt(self.con, self.rt, self.old, [task], from_orch=ghost, force=True)
        self.assertEqual(self.con.execute("SELECT orch_thread FROM tasks WHERE id=?", (task,)).fetchone()[0], ghost)
        self.assertEqual(self.row(q)["orch_id"], ghost)
        self.assertEqual(self.state(q), "current")
        self.assertFalse(self.con.in_transaction)

    def test_adopt_rereads_entry_binding_under_the_write_lock(self):
        """A rebind that lands after adopt's earlier reads but before its write lock must win."""
        counter = []
        self.con.set_trace_callback(lambda sql: counter.append(sql) if sql.startswith("BEGIN IMMEDIATE") else None)
        first, first_task, _ = self.stuck_on_ghost()
        counter.clear()
        core.adopt(self.con, self.rt, self.old, [first_task], from_orch=first, force=True)
        locks = len(counter)  # write transactions a plain adopt takes
        self.assertGreaterEqual(locks, 1)
        other = self.new_orch()
        ghost, task, q = self.stuck_on_ghost()
        seen = []

        def rebind_before_last_lock(sql):
            if sql.startswith("BEGIN IMMEDIATE"):
                seen.append(sql)
                if len(seen) == locks:
                    other_con = store.connect(self.db)
                    try:
                        other_con.execute("UPDATE entries SET orch_id=? WHERE id='desktop'", (other,))
                    finally:
                        other_con.close()

        self.con.set_trace_callback(rebind_before_last_lock)
        core.adopt(self.con, self.rt, self.old, [task], from_orch=ghost, force=True)
        self.con.set_trace_callback(None)
        self.assertEqual(self.con.execute("SELECT orch_id FROM entries WHERE id='desktop'").fetchone()[0], other)
        self.assertNotEqual(self.row(q)["orch_id"], self.old)  # never moved back to the stale owner

    def test_backfill_moves_stuck_rows_backs_up_first_and_is_idempotent(self):
        stuck = self.ask(self.old)
        ghost = self.new_orch()
        self.con.execute("UPDATE entry_messages SET orch_id=? WHERE id=?", (ghost, stuck))
        dry = entry.reconcile_all(self.con, apply=False)
        self.assertEqual(dry["moved"], [stuck])
        self.assertEqual(self.row(stuck)["orch_id"], ghost)  # dry run writes nothing
        backups = Path(self.tmp.name) / "backups"
        done = entry.reconcile_all(self.con, apply=True, backup_dir=backups)
        self.assertEqual(done["moved"], [stuck])
        self.assertTrue(Path(done["backup"]).exists())
        self.assertEqual(self.row(stuck)["orch_id"], self.old)
        self.con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        before = self.db.read_bytes()
        again = entry.reconcile_all(self.con, apply=True, backup_dir=backups)
        self.assertEqual(again["moved"], [])
        self.assertIsNone(again["backup"])
        self.con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.assertEqual(self.db.read_bytes(), before)

    def old_schema_stuck(self):
        """A DB from before #60 (no original_orch_id columns) with one question stranded on a ghost owner."""
        stuck = self.ask(self.old)
        ghost = self.new_orch()
        self.con.execute("UPDATE entry_messages SET orch_id=? WHERE id=?", (ghost, stuck))
        self.con.execute("ALTER TABLE entry_messages DROP COLUMN original_orch_id")
        self.con.execute("ALTER TABLE entry_question_history DROP COLUMN original_orch_id")
        self.con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.con.close()
        return stuck, ghost

    def reconcile_cli(self, *argv):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"ORCHD_HOME": self.tmp.name}), contextlib.redirect_stdout(out):
            code = cli.main(["entry", "reconcile", *argv])
        return code, json.loads(out.getvalue())

    def columns(self):
        con = sqlite3.connect(self.db)
        try:
            return {r[1] for r in con.execute("PRAGMA table_info(entry_messages)")}
        finally:
            con.close()

    def test_reconcile_dry_run_is_read_only_on_an_old_schema_db(self):
        stuck, ghost = self.old_schema_stuck()
        before = {p.name: p.read_bytes() for p in Path(self.tmp.name).iterdir() if p.is_file()}
        code, result = self.reconcile_cli()
        self.assertEqual((code, result["moved"], result["applied"]), (0, [stuck], False))
        self.assertNotIn("original_orch_id", self.columns())  # no schema ALTER
        self.assertEqual({p.name: p.read_bytes() for p in Path(self.tmp.name).iterdir()
                          if p.is_file() and p.name in before}, before)
        self.assertFalse((Path(self.tmp.name) / "backups").exists())
        self.con = store.connect(self.db)  # tearDown closes it

    def test_reconcile_apply_backs_up_before_any_schema_change(self):
        stuck, ghost = self.old_schema_stuck()
        code, result = self.reconcile_cli("--apply")
        self.assertEqual((code, result["moved"], result["applied"]), (0, [stuck], True))
        backup = sqlite3.connect(result["backup"])
        try:
            cols = {r[1] for r in backup.execute("PRAGMA table_info(entry_messages)")}
            owner = backup.execute("SELECT orch_id FROM entry_messages WHERE id=?", (stuck,)).fetchone()[0]
        finally:
            backup.close()
        self.assertNotIn("original_orch_id", cols)  # the backup is the pre-migration file
        self.assertEqual(owner, ghost)
        self.assertIn("original_orch_id", self.columns())
        self.con = store.connect(self.db)
        self.assertEqual(self.row(stuck)["orch_id"], self.old)

    def test_terminal_questions_are_not_moved(self):
        answered = self.ask(self.old)
        self.con.execute("UPDATE entry_messages SET question_state='answered' WHERE id=?", (answered,))
        new = self.new_orch()
        entry.bind(self.con, self.rt, new, force=True)
        self.assertEqual(self.row(answered)["orch_id"], self.old)


if __name__ == "__main__":
    unittest.main()
