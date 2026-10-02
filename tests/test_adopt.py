import tempfile
import unittest
from pathlib import Path

from orchd import core, store
from tests.test_orchd import FakeRuntime


class AdoptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = store.connect(Path(self.tmp.name) / "t.db")
        self.rt = FakeRuntime()
        self.rt.jobs = {"newjob": {}}  # oldjob absent = confirmed dead
        store.register_orch(self.con, "old", "claude", socket="/s/old", session_id="so", job_id="oldjob")
        store.register_orch(self.con, "new", "claude", socket="/s/new", session_id="sn", job_id="newjob")

    def tearDown(self):
        self.tmp.cleanup()

    def task(self, owner="old", repo="demo"):
        t = core.dispatch(self.con, self.rt, orch_thread=owner, repo=repo, title="T", instructions="x",
                          done_when="y", model="sonnet", model_reason="r", task_type="code")
        self.rt.sent.clear()
        return t["id"]

    def inbox_kinds(self, orch):
        return [(m["kind"], m["body"]) for m in core.inbox(self.con, orch)]

    def test_dead_owner_adopt_gives_new_inbox_old_unread_and_summary_without_touching_read_at(self):
        tid = self.task()
        core.progress(self.con, self.rt, tid, "halfway")
        core.ask(self.con, self.rt, tid, "which db?")
        core.inbox(self.con, "old")  # old owner read everything: question is read but unanswered
        core.progress(self.con, self.rt, tid, "still unread")
        before = self.con.execute("SELECT id,read_at FROM messages ORDER BY id").fetchall()
        task_before = dict(store.get_task(self.con, tid))
        self.rt.sent.clear()
        out = core.adopt(self.con, self.rt, "new", [tid])
        self.assertEqual(out["adopted"], [tid])
        after = self.con.execute("SELECT id,read_at FROM messages WHERE id<=?", (before[-1]["id"],)).fetchall()
        self.assertEqual([tuple(r) for r in before], [tuple(r) for r in after])
        task = store.get_task(self.con, tid)
        for col in ("worktree", "branch", "job_id", "session_id", "model", "socket"):
            self.assertEqual(task[col], task_before[col])
        self.assertEqual(task["orch_thread"], "new")
        kinds = self.inbox_kinds("new")
        self.assertIn(("progress", "still unread"), kinds)
        adopt = [b for k, b in kinds if k == "adopt"]
        self.assertEqual(len(adopt), 1)
        self.assertIn("status=question", adopt[0])
        self.assertIn("which db?", adopt[0])
        self.assertNotIn(("question", "which db?"), kinds)  # already read: surfaced via summary only
        self.assertEqual(self.inbox_kinds("old"), [])

    def test_dead_owner_is_not_notified_and_new_owner_is_woken(self):
        tid = self.task()
        core.adopt(self.con, self.rt, "new", [tid])
        self.assertEqual([p for p, _, _ in self.rt.sent], ["/s/new"])
        self.assertIn(tid, self.rt.sent[0][2])

    def test_later_wakes_go_only_to_new_owner(self):
        tid = self.task()
        core.adopt(self.con, self.rt, "new", [tid])
        self.rt.sent.clear()
        core.report(self.con, self.rt, tid, "done", "finished", "ev")
        self.assertEqual([p for p, _, _ in self.rt.sent], ["/s/new"])

    def test_alive_and_unknown_owner_rejected_by_default_and_nothing_changes(self):
        tid = self.task()
        self.rt.jobs = {"newjob": {}, "oldjob": {}}
        with self.assertRaisesRegex(ValueError, "alive"):
            core.adopt(self.con, self.rt, "new", [tid])
        self.rt.jobs = None  # probe failure -> unknown
        self.rt.live_jobs = lambda: (_ for _ in ()).throw(OSError("down"))
        with self.assertRaisesRegex(ValueError, "unknown"):
            core.adopt(self.con, self.rt, "new", [tid])
        self.assertEqual(store.get_task(self.con, tid)["orch_thread"], "old")
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM messages WHERE kind='adopt'").fetchone()[0], 0)
        self.assertEqual(self.rt.sent, [])

    def test_unregistered_owner_is_unknown_and_needs_force(self):
        tid = self.task(owner="ghost")
        with self.assertRaisesRegex(ValueError, "owner_unregistered"):
            core.adopt(self.con, self.rt, "new", [tid])
        core.adopt(self.con, self.rt, "new", [tid], force=True)
        self.assertEqual(store.get_task(self.con, tid)["orch_thread"], "new")

    def test_force_moves_from_alive_owner_and_notifies_old(self):
        tid = self.task()
        self.rt.jobs = {"newjob": {}, "oldjob": {}}
        out = core.adopt(self.con, self.rt, "new", [tid], force=True)
        self.assertTrue(out["forced"])
        self.assertEqual(out["old_owner_notified"], {"old": True})
        self.assertEqual(sorted(p for p, _, _ in self.rt.sent), ["/s/new", "/s/old"])
        old_text = [t for p, _, t in self.rt.sent if p == "/s/old"][0]
        self.assertNotIn("instructions", old_text)

    def test_failed_old_notify_still_moves_once_and_records_evidence(self):
        tid = self.task()
        self.rt.jobs = {"newjob": {}, "oldjob": {}}
        real = self.rt.send_uds

        def flaky(path, session, text):
            if path == "/s/old":
                raise OSError("socket gone")
            real(path, session, text)
        self.rt.send_uds = flaky
        out = core.adopt(self.con, self.rt, "new", [tid], force=True)
        self.assertEqual(out["old_owner_notified"], {"old": False})
        self.assertTrue(out["new_owner_woken"])
        self.assertEqual(store.get_task(self.con, tid)["orch_thread"], "new")
        row = self.con.execute("SELECT wake_error FROM messages WHERE kind='adopt_notice'").fetchone()
        self.assertIn("OSError", row["wake_error"])
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM messages WHERE kind='adopt'").fetchone()[0], 1)
        with self.assertRaisesRegex(ValueError, "already belongs"):
            core.adopt(self.con, self.rt, "new", [tid], force=True)

    def test_new_owner_wake_failure_is_recorded_not_a_failed_transfer(self):
        tid = self.task()
        self.rt.send_uds = lambda *a: (_ for _ in ()).throw(OSError("down"))
        out = core.adopt(self.con, self.rt, "new", [tid])
        self.assertFalse(out["new_owner_woken"])
        self.assertEqual(store.get_task(self.con, tid)["orch_thread"], "new")
        self.assertEqual(store.notification_delivery(self.con, tid)["unread_wake_failed_count"], 1)

    def test_rejects_closed_unknown_stopped_and_dead_targets(self):
        tid = self.task()
        closed = self.task()
        core.close(self.con, self.rt, closed)
        with self.assertRaisesRegex(ValueError, "closed"):
            core.adopt(self.con, self.rt, "new", [closed])
        with self.assertRaises(KeyError):
            core.adopt(self.con, self.rt, "new", ["nope"])
        with self.assertRaisesRegex(ValueError, "unknown orch"):
            core.adopt(self.con, self.rt, "nobody", [tid])
        self.rt.jobs = {}  # new owner now confirmed dead too
        with self.assertRaisesRegex(ValueError, "dead"):
            core.adopt(self.con, self.rt, "new", [tid])
        self.rt.jobs = {"newjob": {}}
        store.stop_orch(self.con, "new")
        with self.assertRaisesRegex(ValueError, "stopped"):
            core.adopt(self.con, self.rt, "new", [tid])
        self.assertEqual(store.get_task(self.con, tid)["orch_thread"], "old")

    def test_unknown_target_is_allowed(self):
        tid = self.task()
        store.register_orch(self.con, "cx", "codex")
        out = core.adopt(self.con, self.rt, "cx", [tid])
        self.assertEqual(out["new_owner_health"]["state"], "unknown")
        self.assertEqual(self.rt.woken[0][1], "cx")

    def test_selection_is_explicit_and_never_grabs_other_groups(self):
        a, b = self.task(), self.task()
        other = self.task(owner="third")
        with self.assertRaisesRegex(ValueError, "name the task ids"):
            core.adopt(self.con, self.rt, "new")
        with self.assertRaisesRegex(ValueError, "not old"):
            core.adopt(self.con, self.rt, "new", [other], from_orch="old")
        core.adopt(self.con, self.rt, "new", [a])
        self.assertEqual(store.get_task(self.con, b)["orch_thread"], "old")
        core.adopt(self.con, self.rt, "new", from_orch="old")
        self.assertEqual(store.get_task(self.con, b)["orch_thread"], "new")
        self.assertEqual(store.get_task(self.con, other)["orch_thread"], "third")

    def test_batch_with_one_bad_task_moves_nothing(self):
        ok = self.task()
        closed = self.task()
        core.close(self.con, self.rt, closed)
        with self.assertRaises(ValueError):
            core.adopt(self.con, self.rt, "new", [ok, closed])
        self.assertEqual(store.get_task(self.con, ok)["orch_thread"], "old")

    def test_transaction_failure_rolls_back_every_move_and_event(self):
        a, b = self.task(), self.task()
        real = store.add_message
        calls = []

        def boom(con, task_id, kind, body, evidence=None):
            calls.append(task_id)
            if len(calls) == 2:
                raise RuntimeError("disk full")
            return real(con, task_id, kind, body, evidence)
        store.add_message = boom
        try:
            with self.assertRaises(RuntimeError):
                core.adopt(self.con, self.rt, "new", [a, b])
        finally:
            store.add_message = real
        self.assertEqual({store.get_task(self.con, t)["orch_thread"] for t in (a, b)}, {"old"})
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM messages WHERE kind='adopt'").fetchone()[0], 0)
        self.assertFalse(self.con.in_transaction)
        self.assertEqual(self.rt.sent, [])

    def test_stale_owner_in_transaction_is_refused(self):
        tid = self.task()
        with self.assertRaisesRegex(ValueError, "changed owner"):
            store.move_task_orch(self.con, [(tid, "someone-else", "b", "e")], "new")
        self.assertEqual(store.get_task(self.con, tid)["orch_thread"], "old")

    def test_list_open_does_not_leak_adopt_body(self):
        tid = self.task()
        core.ask(self.con, self.rt, tid, "SECRET question")
        core.adopt(self.con, self.rt, "new", [tid])
        self.assertNotIn("SECRET", str(core.list_open(self.con, self.rt)))


if __name__ == "__main__":
    unittest.main()
