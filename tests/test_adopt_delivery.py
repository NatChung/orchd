"""Additional fault boundaries for PR27; all data and transports are disposable."""
import json
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

from orchd import core, store, watch
from tests import test_adopt_review_regressions as review


class DeliveryTest(unittest.TestCase):
    setUp = review.Review.setUp
    tearDown = review.Review.tearDown
    owner = review.Review.owner
    targets = review.Review.targets

    def test_inflight_report_routes_to_new_owner_and_preserves_queued_answers(self):
        queued = store.add_message(self.con, "task", store.QUEUED, "PRIVATE pending answer")
        fetched, adopted = threading.Event(), threading.Event()
        original = store.add_message
        errors = []

        def paused(con, task_id, kind, body, evidence=None):
            if kind == "report":
                fetched.set()
                if not adopted.wait(5):
                    raise TimeoutError("adopt")
            return original(con, task_id, kind, body, evidence)

        def report():
            con = store.connect(self.db)
            try:
                core.report(con, self.rt, "task", "done", "finished", "PRIVATE evidence")
            except Exception as error:
                errors.append(error)
            finally:
                con.close()

        with patch.object(store, "add_message", paused):
            thread = threading.Thread(target=report)
            thread.start()
            try:
                self.assertTrue(fetched.wait(5))
                core.adopt(self.con, self.rt, "new", ["task"], force=True)
                self.rt.commands.clear()
            finally:
                adopted.set()
                thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.targets(), ["new"])
        self.assertEqual(store.get_task(self.con, "task")["status"], "done")
        self.assertEqual([m["id"] for m in store.pending_answers(self.con, "task")], [queued])
        self.assertEqual(core.inbox(self.con, "old"), [])
        self.assertNotIn(store.QUEUED, [m["kind"] for m in core.inbox(self.con, "new")])
        self.assertEqual(store.pending_answers(self.con, "task")[0]["body"], "PRIVATE pending answer")

    def test_multigroup_batch_notice_failure_is_committed_queryable_and_new_wakes(self):
        store.register_orch(self.con, "old2", "codex")
        store.create_task(self.con, id="task2", repo="scratch", repo_path=self.tmp.name,
                          title="second", instructions="PRIVATE", done_when="x", orch_thread="old2",
                          codex_bin="/mock/codex", status="running")
        self.con.execute("CREATE TRIGGER fail_notice BEFORE INSERT ON messages WHEN NEW.kind='adopt_notice' "
                         "BEGIN SELECT RAISE(ABORT, 'PRIVATE notice failure'); END")
        out = core.adopt(self.con, self.rt, "new", ["task", "task2"], force=True)
        self.assertTrue(out["committed"])
        self.assertEqual(out["adopted"], ["task", "task2"])
        self.assertEqual(out["old_owner_notified"], {"old": False, "old2": False})
        self.assertTrue(out["new_owner_woken"])
        self.assertEqual(self.targets(), ["new"])
        for task_id, old in (("task", "old"), ("task2", "old2")):
            row = store.latest_message(self.con, task_id, "adopt")
            self.assertEqual(row["recipient_orch"], "new")
            self.assertEqual(json.loads(row["evidence"])["recipient_orch"], "new")
            metadata = store.notification_delivery(self.con, task_id)
            self.assertEqual(metadata["notice_wake_failed_count"], 1)
            self.assertEqual(metadata["latest_notice_wake_failure"]["recipient_orch"], old)
            self.assertEqual(metadata["latest_notice_wake_failure"]["error_type"], "IntegrityError")
            self.assertNotIn("PRIVATE", str(metadata))
        self.assertNotIn("PRIVATE", str(core.list_open(self.con, self.rt)))

    def test_failed_old_notice_history_keeps_recipient_after_read_and_another_move(self):
        def fail_old(cmd, **kwargs):
            if cmd[cmd.index("--thread") + 1] == "old":
                raise OSError("PRIVATE failure")
            self.rt.commands.append(cmd)
        self.rt.run = fail_old
        core.adopt(self.con, self.rt, "new", ["task"], force=True)
        notice = store.latest_message(self.con, "task", "adopt_notice")
        core.inbox(self.con, "new")
        core.adopt(self.con, self.rt, "other", ["task"], force=True)
        row = next(r for r in self.con.execute(watch.QUERY, (0,)) if r["id"] == notice["id"])
        self.assertEqual(row["orch_thread"], "old")
        self.assertEqual(store.notification_delivery(self.con, "task")["latest_notice_wake_failure"]["recipient_orch"], "old")
        self.assertIsNone(store.latest_message(self.con, "task", "adopt_notice")["read_at"])
        self.assertNotIn("adopt_notice", [m["kind"] for m in core.inbox(self.con, "other")])

    def test_changed_old_registry_cannot_bypass_default_reject(self):
        self.con.execute("UPDATE orchs SET kind='claude',job_id='absent' WHERE id='old'")
        original = store.move_task_orch

        def changed(con, moves, target):
            self.peer.execute("UPDATE orchs SET job_id='newjob' WHERE id='old'")
            return original(con, moves, target)
        with patch.object(store, "move_task_orch", changed):
            with self.assertRaisesRegex(ValueError, "old owner registry changed"):
                core.adopt(self.con, self.rt, "new", ["task"])
        self.assertEqual(self.owner(), "old")
        self.assertEqual(self.targets(), [])

    def test_transport_holds_task_lock_across_processes_but_not_sqlite_write_lock(self):
        sending, release = threading.Event(), threading.Event()
        errors = []

        def slow(cmd, **kwargs):
            sending.set()
            if not release.wait(5):
                raise TimeoutError("release transport")
            self.rt.commands.append(cmd)
        self.rt.run = slow

        def progress():
            con = store.connect(self.db)
            try:
                core.progress(con, self.rt, "task", "slow delivery")
            except Exception as error:
                errors.append(error)
            finally:
                con.close()
        thread = threading.Thread(target=progress)
        thread.start()
        try:
            self.assertTrue(sending.wait(5))
            # Actual cross-process contention, with a bounded zero-wait probe.
            probe = subprocess.run([sys.executable, "-c",
                "import sys; from orchd import store; c=store.connect(sys.argv[1]); "
                "\ntry:\n with store.task_delivery(c,['task'],timeout=0): sys.exit(2)"
                "\nexcept TimeoutError: sys.exit(0)", str(self.db)], timeout=5, capture_output=True)
            self.assertEqual(probe.returncode, 0, probe.stderr)
            with self.assertRaises(TimeoutError):
                with store.task_delivery(self.peer, ["task"], timeout=0):
                    pass
            # Queue writes do not need this task's transport lock or a held DB transaction.
            queued = store.add_message(self.peer, "task", store.QUEUED, "PRIVATE queued during transport")
            store.create_task(self.peer, id="independent", repo="scratch", repo_path=self.tmp.name,
                              title="independent", instructions="x", done_when="x", orch_thread="old",
                              codex_bin="/mock/codex", status="running")
            with store.task_delivery(self.peer, ["independent"], timeout=0), store.immediate(self.peer):
                store.update_task(self.peer, "independent", status="done")
        finally:
            release.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        core.adopt(self.con, self.rt, "new", ["task"], force=True)
        self.assertEqual(store.pending_answers(self.con, "task")[0]["id"], queued)


if __name__ == "__main__":
    unittest.main()
