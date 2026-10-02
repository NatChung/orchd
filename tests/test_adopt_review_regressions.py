"""Independent PR27 review. Temp DBs; real UDS, mocked Codex process transport. No UI proof."""
import json
import socket
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from orchd import core, store, watch
from orchd.runtime import Runtime


class IsolatedRuntime(Runtime):
    def __init__(self):
        self.jobs = {"newjob": {}, "otherjob": {}}
        self.commands = []
        self.on_probe = None

    def live_jobs(self):
        if self.on_probe:
            self.on_probe()
        return self.jobs

    def run(self, cmd, **kwargs):
        self.commands.append(cmd)


class Review(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ac2416c8-")
        self.db = Path(self.tmp.name) / "review.db"
        self.con = store.connect(self.db)
        self.peer = store.connect(self.db)
        self.rt = IsolatedRuntime()
        for name in ("old", "new", "other"):
            store.register_orch(self.con, name, "codex")
        store.create_task(self.con, id="task", repo="scratch", repo_path=self.tmp.name,
                          title="isolated", instructions="PRIVATE instructions", done_when="x",
                          orch_thread="old", codex_bin="/mock/codex", status="question",
                          worktree="unchanged", job_id="unchanged", model="gpt-6.1-sol")

    def tearDown(self):
        self.peer.close()
        self.con.close()
        self.tmp.cleanup()

    def owner(self):
        return store.get_task(self.con, "task")["orch_thread"]

    def targets(self):
        return [c[c.index("--thread") + 1] for c in self.rt.commands]

    def test_unknown_old_requires_force_and_unknown_registered_target_allowed(self):
        with self.assertRaisesRegex(ValueError, "unknown"):
            core.adopt(self.con, self.rt, "new", ["task"])
        out = core.adopt(self.con, self.rt, "new", ["task"], force=True)
        self.assertEqual(out["new_owner_health"]["state"], "unknown")
        self.assertEqual(self.targets(), ["old", "new"])

    def test_alive_default_reject_and_dead_direct(self):
        self.con.execute("UPDATE orchs SET kind='claude',job_id='oldjob' WHERE id='old'")
        self.rt.jobs["oldjob"] = {}
        with self.assertRaisesRegex(ValueError, "alive"):
            core.adopt(self.con, self.rt, "new", ["task"])
        del self.rt.jobs["oldjob"]
        core.adopt(self.con, self.rt, "new", ["task"])
        self.assertEqual(self.targets(), ["new"])

    def test_read_question_report_summary_unread_and_private_metadata(self):
        q = store.add_message(self.con, "task", "question", "PRIVATE question")
        r = store.add_message(self.con, "task", "report", "PRIVATE report")
        store.mark_read(self.con, [q, r])
        u = store.add_message(self.con, "task", "progress", "unread")
        before = dict(self.con.execute("SELECT id,read_at FROM messages"))
        core.adopt(self.con, self.rt, "new", ["task"], force=True)
        after = dict(self.con.execute("SELECT id,read_at FROM messages WHERE id<=?", (u,)))
        self.assertEqual(before, after)
        self.assertEqual(core.inbox(self.con, "old"), [])
        inbox = core.inbox(self.con, "new")
        self.assertEqual([m["kind"] for m in inbox], ["progress", "adopt"])
        self.assertIn("PRIVATE question", inbox[-1]["body"])
        self.assertIn("PRIVATE report", inbox[-1]["body"])
        public = str(store.notification_delivery(self.con, "task"))
        public += str(core.list_open(self.con, self.rt))
        self.assertNotIn("PRIVATE", public)

    def test_batch_event_failure_rollback_and_stale_owner_cas(self):
        store.create_task(self.con, id="task2", repo="scratch", repo_path=self.tmp.name,
                          title="two", instructions="x", done_when="x", orch_thread="old",
                          codex_bin="/mock/codex", status="running")
        self.con.execute("CREATE TRIGGER fail_adopt BEFORE INSERT ON messages "
                         "WHEN NEW.task_id='task2' AND NEW.kind='adopt' "
                         "BEGIN SELECT RAISE(ABORT, 'injected'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            core.adopt(self.con, self.rt, "new", ["task", "task2"], force=True)
        self.assertEqual({t["orch_thread"] for t in store.open_tasks(self.con)}, {"old"})
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)
        self.assertEqual(self.targets(), [])
        with self.assertRaisesRegex(ValueError, "changed owner"):
            store.move_task_orch(self.con, [("task", "wrong", "x", "x")], "new")
        self.assertEqual(self.owner(), "old")

    def test_unknown_closed_selection_and_stopped_target_reject(self):
        for target, ids in (("missing", ["task"]), ("new", []), ("new", ["missing"])):
            with self.assertRaises((ValueError, KeyError)):
                core.adopt(self.con, self.rt, target, ids, force=True)
        store.update_task(self.con, "task", status="closed")
        with self.assertRaisesRegex(ValueError, "closed"):
            core.adopt(self.con, self.rt, "new", ["task"], force=True)
        store.update_task(self.con, "task", status="running")
        store.stop_orch(self.con, "new")
        with self.assertRaisesRegex(ValueError, "stopped"):
            core.adopt(self.con, self.rt, "new", ["task"], force=True)
        self.assertEqual(self.owner(), "old")

    def test_real_uds_routes_after_commit_later_wakes_new_only(self):
        sockpath = str(Path(self.tmp.name) / "new.sock")
        server = socket.socket(socket.AF_UNIX)
        server.bind(sockpath)
        server.listen(2)
        server.settimeout(3)
        frames, owners, errors = [], [], []

        def collect():
            check = store.connect(self.db)
            try:
                for _ in range(2):
                    peer, _ = server.accept()
                    with peer:
                        data = bytearray()
                        while True:
                            chunk = peer.recv(65536)
                            if not chunk:
                                break
                            data.extend(chunk)
                    frames.append(json.loads(data))
                    owners.append(store.get_task(check, "task")["orch_thread"])
            except Exception as exc:
                errors.append(str(exc))
            finally:
                check.close()

        self.con.execute("UPDATE orchs SET kind='claude',socket=?,session_id='scratch-session',"
                         "job_id='newjob' WHERE id='new'", (sockpath,))
        collector = threading.Thread(target=collect)
        collector.start()
        try:
            core.adopt(self.con, self.rt, "new", ["task"], force=True)
            self.rt.commands.clear()
            core.progress(self.con, self.rt, "task", "later")
            collector.join(5)
            self.assertFalse(collector.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(owners, ["new", "new"])
            self.assertEqual([f["session_id"] for f in frames], ["scratch-session"] * 2)
            self.assertIn("adopted", frames[0]["message"]["content"])
            self.assertIn("later", frames[1]["message"]["content"])
            self.assertEqual(self.targets(), [])
        finally:
            server.close()

    def test_old_notice_failure_record_queryable_private(self):
        def fail_old(cmd, **kwargs):
            if cmd[cmd.index("--thread") + 1] == "old":
                raise OSError("PRIVATE failure")
            self.rt.commands.append(cmd)
        self.rt.run = fail_old
        out = core.adopt(self.con, self.rt, "new", ["task"], force=True)
        self.assertFalse(out["old_owner_notified"]["old"])
        row = self.con.execute("SELECT * FROM messages WHERE kind='adopt_notice'").fetchone()
        self.assertEqual(json.loads(row["evidence"])["old_orch"], "old")
        self.assertIn("PRIVATE failure", row["wake_error"])
        self.assertNotIn("PRIVATE", str(core.list_open(self.con, self.rt)))

    def test_two_adopts_same_precommit_snapshot_one_cas_winner(self):
        ready = threading.Barrier(2)
        results = []
        original = store.move_task_orch
        def both_prevalidated(con, moves, target):
            ready.wait(5)
            return original(con, moves, target)
        def call(target):
            con = store.connect(self.db)
            rt = IsolatedRuntime()
            try:
                results.append((target, core.adopt(con, rt, target, ["task"], force=True), rt.commands))
            except Exception as exc:
                results.append((target, type(exc).__name__, rt.commands))
            finally:
                con.close()
        with patch.object(store, "move_task_orch", both_prevalidated):
            threads = [threading.Thread(target=call, args=(target,)) for target in ("new", "other")]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(8)
            self.assertFalse(any(thread.is_alive() for thread in threads))
        winners = [r for r in results if isinstance(r[1], dict)]
        losers = [r for r in results if r[1] == "ValueError"]
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(losers), 1)
        self.assertEqual(losers[0][2], [])
        self.assertEqual(self.owner(), winners[0][0])

    def test_target_stopped_after_validation_must_reject(self):
        self.rt.on_probe = lambda: store.stop_orch(self.peer, "new")
        with self.assertRaisesRegex(ValueError, "stopped"):
            core.adopt(self.con, self.rt, "new", ["task"], force=True)
        self.assertEqual(self.owner(), "old")

    def test_interleaved_postcommit_adopts_do_not_send_stale_acquisition_wake(self):
        first_committed = threading.Event()
        second_finished = threading.Event()
        errors = []
        original = store.move_task_orch
        def paused(con, moves, target):
            ids = original(con, moves, target)
            if target == "new":
                first_committed.set()
                if not second_finished.wait(5):
                    raise TimeoutError("second adopt")
            return ids
        def first():
            con = store.connect(self.db)
            try:
                core.adopt(con, self.rt, "new", ["task"], force=True)
            except Exception as exc:
                errors.append(str(exc))
            finally:
                con.close()
        with patch.object(store, "move_task_orch", paused):
            thread = threading.Thread(target=first)
            thread.start()
            self.assertTrue(first_committed.wait(5))
            core.adopt(self.con, self.rt, "other", ["task"], force=True)
            second_finished.set()
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.owner(), "other")
        stale = [c for c in self.rt.commands if c[c.index("--thread") + 1] == "new"
                 and "adopted" in c[-1]]
        self.assertEqual(stale, [], "first caller sends acquired wake after task moved to other")

    def test_postcommit_notice_insert_failure_must_return_explicit_committed_outcome(self):
        self.con.execute("CREATE TRIGGER fail_notice BEFORE INSERT ON messages "
                         "WHEN NEW.kind='adopt_notice' BEGIN SELECT RAISE(ABORT, 'notice failure'); END")
        try:
            out = core.adopt(self.con, self.rt, "new", ["task"], force=True)
        except sqlite3.IntegrityError as exc:
            events = self.con.execute("SELECT COUNT(*) FROM messages WHERE kind='adopt'").fetchone()[0]
            self.fail(f"raised {exc}; owner={self.owner()}, committed_adopt_events={events}, wakes={self.targets()}")
        self.assertEqual(out["adopted"], ["task"])
        self.assertFalse(out["old_owner_notified"]["old"])
        self.assertTrue(out["new_owner_woken"])

    def test_old_notice_is_grouped_under_old_recipient(self):
        def fail_old(cmd, **kwargs):
            if cmd[cmd.index("--thread") + 1] == "old":
                raise OSError("old notify unavailable")
            self.rt.commands.append(cmd)
        self.rt.run = fail_old
        core.adopt(self.con, self.rt, "new", ["task"], force=True)
        rows = self.con.execute(watch.QUERY, (0,)).fetchall()
        notice = next(row for row in rows if row["kind"] == "adopt_notice")
        self.assertEqual(json.loads(notice["evidence"])["old_orch"], "old")
        self.assertEqual(notice["orch_thread"], "old", watch.format_row(notice, color=False))

    def test_inflight_worker_progress_after_commit_wakes_current_owner(self):
        fetched = threading.Event()
        adopted = threading.Event()
        original = store.add_message
        errors = []
        def pause_message(con, task_id, kind, body, evidence=None):
            if kind == "progress":
                fetched.set()
                if not adopted.wait(5):
                    raise TimeoutError("adopt")
            return original(con, task_id, kind, body, evidence)
        def progress():
            con = store.connect(self.db)
            try:
                core.progress(con, self.rt, "task", "later worker update")
            except Exception as exc:
                errors.append(str(exc))
            finally:
                con.close()
        with patch.object(store, "add_message", pause_message):
            thread = threading.Thread(target=progress)
            thread.start()
            self.assertTrue(fetched.wait(5))
            core.adopt(self.con, self.rt, "new", ["task"], force=True)
            self.rt.commands.clear()
            adopted.set()
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.owner(), "new")
        self.assertEqual(self.targets(), ["new"], "progress written after adopt commit wakes stale old owner")


if __name__ == "__main__":
    unittest.main(verbosity=2)
