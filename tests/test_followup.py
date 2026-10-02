"""Issue #5 (followup): add an instruction to an open task; same worker, worktree, session; delivery rides on answer."""
import contextlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchd import core, mcp_server, store
from tests.test_orchd import FakeRuntime


class FollowupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = store.connect(Path(self.tmp.name) / "t.db")
        self.rt = FakeRuntime()
        self.rt.sleep = lambda s: None

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def dispatch(self, model="sonnet"):
        t = core.dispatch(self.con, self.rt, orch_thread="thread-A", repo="demo", title="T", instructions="do it",
                          done_when="tests pass", model=model, model_reason="r", task_type="code")
        self.rt.sent.clear()  # the task message itself
        return t

    def rows(self, task_id, kind):
        return self.con.execute("SELECT * FROM messages WHERE task_id=? AND kind=? ORDER BY id", (task_id, kind)).fetchall()

    def events(self, task_id):
        return [json.loads(r["body"]) for r in self.rows(task_id, "followup")]

    def test_missing_and_closed_are_refused_without_events_or_sends(self):
        with self.assertRaises(KeyError):
            core.followup(self.con, self.rt, "nope", "x")
        t = self.dispatch()
        core.close(self.con, self.rt, t["id"])
        with self.assertRaisesRegex(ValueError, "closed"):
            core.followup(self.con, self.rt, t["id"], "more")
        self.assertEqual((self.rt.sent, self.rt.resumed, self.events(t["id"])), ([], [], []))

    def test_empty_message_refused(self):
        t = self.dispatch()
        for bad in ("", "  \n", None):
            with self.assertRaisesRegex(ValueError, "non-empty"):
                core.followup(self.con, self.rt, t["id"], bad)
        self.assertEqual(self.rt.sent, [])

    def test_claude_done_task_same_worker_session_and_report_kept(self):
        t = self.dispatch()
        core.report(self.con, self.rt, t["id"], "done", "first pass", "ev1")
        before = store.get_task(self.con, t["id"])
        out = core.followup(self.con, self.rt, t["id"], "also add docs")
        self.assertEqual(out, dict(status="delivered", delivered=1, pending=0))
        (path, session, text), = self.rt.sent
        self.assertEqual(len(self.rt.sent), 1)
        self.assertEqual((path, session), (before["socket"], before["session_id"]))
        self.assertIn("[orchd answer " + t["id"] + "]", text)
        self.assertIn("[followup]", text)
        self.assertTrue(text.endswith("also add docs"))
        after = store.get_task(self.con, t["id"])
        for col in ("model", "worktree", "branch", "socket", "session_id", "job_id", "orch_thread"):
            self.assertEqual(after[col], before[col], col)
        self.assertEqual(self.rt.stopped, [])
        self.assertEqual(after["status"], "acked")  # working again; the first report row is untouched
        (report,) = self.rows(t["id"], "report")
        self.assertEqual((report["body"], report["evidence"]), ("done: first pass", "ev1"))
        (event,) = self.events(t["id"])
        self.assertEqual((event["message"], event["from_status"], event["status"], event["session_id"]),
                         ("also add docs", "done", "delivered", before["session_id"]))
        self.assertEqual(len(self.rows(t["id"], "answer")), 1)

    def test_second_report_after_followup_wakes_orch_and_keeps_both(self):
        t = self.dispatch()
        core.report(self.con, self.rt, t["id"], "done", "first", "e1")
        core.followup(self.con, self.rt, t["id"], "again")
        core.ack(self.con, t["id"])
        core.report(self.con, self.rt, t["id"], "done", "second", "e2")
        inbox = core.inbox(self.con, "thread-A")
        self.assertEqual([m["body"] for m in inbox if m["kind"] == "report"], ["done: first", "done: second"])
        self.assertNotIn("followup", [m["kind"] for m in inbox])  # event log only

    def test_claude_send_failure_is_visible_and_recorded(self):
        t = self.dispatch()

        def boom(*a):
            raise OSError("socket gone")
        self.rt.send_uds = boom
        with self.assertRaisesRegex(OSError, "socket gone"):
            core.followup(self.con, self.rt, t["id"], "x")
        (event,) = self.events(t["id"])
        self.assertEqual(event["status"], "failed")
        self.assertIn("socket gone", event["error"])
        self.assertEqual(self.rows(t["id"], "answer"), [])  # not claimed as delivered

    def test_claude_busy_lock_is_not_accepted(self):
        t = self.dispatch()
        orig = store.task_delivery

        def busy(*a, **k):
            raise TimeoutError("lock")
        store.task_delivery = busy
        try:
            with self.assertRaises(TimeoutError):
                core.followup(self.con, self.rt, t["id"], "later")
        finally:
            store.task_delivery = orig
        self.assertEqual((self.events(t["id"]), store.pending_answers(self.con, t["id"]), self.rt.sent), ([], [], []))

    def test_busy_codex_queues_fifo_then_flushes_once_without_respawn(self):
        t = self.dispatch("sol")
        self.rt.alive_pids = {"4242"}
        self.assertEqual(core.answer(self.con, self.rt, t["id"], "plain answer")["pending"], 1)
        self.assertEqual(core.followup(self.con, self.rt, t["id"], "do more"),
                         dict(status="queued", delivered=0, pending=2))
        self.assertEqual(self.rt.resumed, [])  # never resumed over a live turn
        self.rt.alive_pids = set()
        self.assertEqual(core.answer(self.con, self.rt, t["id"], flush=True), dict(status="delivered", delivered=2, pending=0))
        (thread, message, _model), = self.rt.resumed
        self.assertEqual(thread, "thread-W")
        self.assertLess(message.index("plain answer"), message.index("do more"))
        self.assertEqual(core.answer(self.con, self.rt, t["id"], flush=True)["delivered"], 0)
        self.assertEqual(len(self.rt.resumed), 1)
        self.assertEqual([e["status"] for e in self.events(t["id"])], ["queued"])
        self.assertEqual(store.get_task(self.con, t["id"])["session_id"], "thread-W")

    def test_idle_codex_failed_resume_keeps_instruction_queued(self):
        t = self.dispatch("sol")

        def fail(*a):
            raise RuntimeError("codex down")
        self.rt.resume_codex_worker = fail
        out = core.followup(self.con, self.rt, t["id"], "go on")
        self.assertEqual((out["status"], out["pending"]), ("failed", 1))
        self.assertEqual(self.events(t["id"])[0]["status"], "failed")
        self.assertEqual(len(store.pending_answers(self.con, t["id"])), 1)

    def test_mcp_tool(self):
        tool = next(x for x in mcp_server.TOOLS if x["name"] == "followup")
        self.assertEqual(tool["inputSchema"]["required"], ["task_id", "message"])
        t = self.dispatch()
        out = io.StringIO()
        meta = {"threadId": "thread-A"}
        calls = [{"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": "followup", "arguments": a, "_meta": meta}}
                 for i, a in enumerate(({"task_id": t["id"], "message": "next"}, {"task_id": "nope", "message": "x"}), 1)]
        mcp_server.serve(io.StringIO("\n".join(json.dumps(c) for c in calls) + "\n"), out, self.con, self.rt)
        ok, missing = [json.loads(l)["result"] for l in out.getvalue().splitlines()]
        self.assertEqual(json.loads(ok["content"][0]["text"])["status"], "delivered")
        self.assertTrue(missing["isError"])


REPO_ROOT = str(Path(__file__).resolve().parent.parent)


class FollowupAcceptanceTest(unittest.TestCase):
    """PR #32 review 94c5cd97: the acceptance record is written under the task lock, before transport, and the
    receipt never claims less (or more) than what happened."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "t.db"
        self.con = store.connect(self.db)
        self.rt = FakeRuntime()
        self.rt.sleep = lambda s: None

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def done_task(self, model="sonnet"):
        t = core.dispatch(self.con, self.rt, orch_thread="thread-A", repo="demo", title="T", instructions="do it",
                          done_when="pass", model=model, model_reason="r", task_type="code")
        self.rt.sent.clear()
        core.report(self.con, self.rt, t["id"], "done", "prior result", "prior evidence")
        return store.get_task(self.con, t["id"])

    def events(self, t):
        return [json.loads(r[0]) for r in self.con.execute(
            "SELECT body FROM messages WHERE task_id=? AND kind='followup' ORDER BY id", (t["id"],))]

    @contextlib.contextmanager
    def holder(self, t):
        """Another process holding the task's real flock; `close` stage completes a close while it holds it."""
        code = (
            "import sys\n"
            "from orchd import core,store\n"
            "from tests.test_orchd import FakeRuntime\n"
            "c=store.connect(sys.argv[1])\n"
            "with store.task_delivery(c,[sys.argv[2]]):\n"
            " print('locked',flush=True)\n"
            " while True:\n"
            "  action=sys.stdin.readline().strip()\n"
            "  if action=='close': core._close_locked(c,FakeRuntime(),sys.argv[2],None,None)\n"
            "  print('ready',flush=True)\n"
            "  if action in ('','release'): break\n"
            "c.close()\n")
        p = subprocess.Popen([sys.executable, "-c", code, str(self.db), t["id"]], cwd=REPO_ROOT,
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(p.stdout.readline().strip(), "locked")
            yield p
        finally:
            p.communicate("release\nrelease\n", timeout=10)
            self.assertEqual(p.returncode, 0)

    def stage(self, p, action):
        p.stdin.write(action + "\n")
        p.stdin.flush()
        self.assertEqual(p.stdout.readline().strip(), "ready")

    def test_transport_error_mentioning_closed_still_records_a_failed_event(self):
        t = self.done_task()

        def boom(*a):
            raise OSError("connection closed")
        self.rt.send_uds = boom
        with self.assertRaisesRegex(OSError, "connection closed"):
            core.followup(self.con, self.rt, t["id"], "keep this instruction")
        (event,) = self.events(t)
        self.assertEqual(event["status"], "failed")
        self.assertIn("connection closed", event["error"])

    def release(self, p):
        self.stage(p, "release")
        p.wait(10)

    def test_lock_held_by_close_in_progress_is_not_accepted_then_close_finishes(self):
        for model in ("sonnet", "sol"):
            with self.subTest(model=model):
                t = self.done_task(model)
                real = store.task_delivery
                with self.holder(t) as p:
                    self.stage(p, "hold")  # close has the lock (worker stopped) but status is not closed yet
                    with patch.object(store, "task_delivery", lambda c, ids, timeout=65: real(c, ids, timeout=0.05)):
                        with self.assertRaises(TimeoutError):
                            core.followup(self.con, self.rt, t["id"], "wait for me")
                    self.assertEqual(store.get_task(self.con, t["id"])["status"], "done")
                    self.assertEqual((self.rt.sent, self.rt.resumed, self.events(t),
                                      store.pending_answers(self.con, t["id"])), ([], [], [], []))
                    self.stage_close_after_hold(p)
                self.assertEqual(store.get_task(self.con, t["id"])["status"], "closed")
                self.assertEqual((self.events(t), store.pending_answers(self.con, t["id"])), ([], []))  # nothing stranded

    def stage_close_after_hold(self, p):
        """The holder is already past its 'hold' stage; finish the close it had started, then it exits."""
        p.stdin.write("close\n")
        p.stdin.flush()
        self.assertEqual(p.stdout.readline().strip(), "ready")

    def test_close_completing_while_followup_waits_for_lock_is_refused(self):
        for model in ("sonnet", "sol"):
            with self.subTest(model=model):
                t = self.done_task(model)
                real_get, calls = store.get_task, []
                with self.holder(t) as p:
                    def stale_get(c, task_id):  # first reads see `done`; close lands, lock is released, then we lock
                        row = real_get(c, task_id)
                        calls.append(1)
                        if len(calls) == (2 if model == "sol" else 1):
                            self.stage(p, "close")
                            self.release(p)
                        return row
                    with patch.object(store, "get_task", stale_get):
                        with self.assertRaisesRegex(ValueError, "closed"):
                            core.followup(self.con, self.rt, t["id"], "late")
                self.assertEqual(real_get(self.con, t["id"])["status"], "closed")
                self.assertEqual((self.rt.sent, self.rt.resumed, self.events(t),
                                  store.pending_answers(self.con, t["id"])), ([], [], [], []))

    def test_codex_lock_busy_after_acceptance_returns_accepted_queued_receipt_and_flush_runs_it_once(self):
        t = self.done_task("sol")
        real, calls = store.task_delivery, []

        def second_busy(c, ids, timeout=65):
            calls.append(1)
            if len(calls) == 2:  # acceptance took the lock; the delivery attempt finds it taken
                raise TimeoutError("busy")
            return real(c, ids, timeout=timeout)
        with patch.object(store, "task_delivery", second_busy):
            result = core.followup(self.con, self.rt, t["id"], "accepted and pending")
        self.assertEqual((result["status"], result["delivered"], result["pending"]), ("queued", 0, 1))
        self.assertIn("busy", result["error"])
        (event,) = self.events(t)
        self.assertEqual((event["status"], event["pending"]), ("queued", 1))
        self.assertEqual(core.answer(self.con, self.rt, t["id"], flush=True),
                         dict(status="delivered", delivered=1, pending=0))
        self.assertEqual(len(self.rt.resumed), 1)
        self.assertTrue(self.rt.resumed[0][1].endswith("accepted and pending"))

    def test_acceptance_write_failure_sends_nothing(self):
        t = self.done_task()
        self.con.execute("CREATE TRIGGER fail_event BEFORE INSERT ON messages WHEN NEW.kind='followup' "
                         "BEGIN SELECT RAISE(ABORT,'injected accept failure'); END")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected accept failure"):
            core.followup(self.con, self.rt, t["id"], "not accepted")
        self.assertEqual((self.rt.sent, store.get_task(self.con, t["id"])["status"]), ([], "done"))
        self.assertEqual(self.events(t), [])

    def test_record_update_failure_after_delivery_keeps_accepted_event_and_delivered_receipt(self):
        t = self.done_task()
        self.con.execute("CREATE TRIGGER fail_update BEFORE UPDATE ON messages WHEN NEW.kind='followup' "
                         "BEGIN SELECT RAISE(ABORT,'injected record failure'); END")
        result = core.followup(self.con, self.rt, t["id"], "delivered but record lags")
        self.assertEqual((result["status"], result["delivered"], result["pending"]), ("delivered", 1, 0))
        self.assertIn("injected record failure", result["record_error"])
        self.assertEqual(len(self.rt.sent), 1)
        self.assertEqual(store.get_task(self.con, t["id"])["status"], "acked")
        (event,) = self.events(t)
        self.assertEqual((event["status"], event["message"]), ("accepted", "delivered but record lags"))

    def test_mcp_followup_description_has_question_flush_exception(self):
        tool = next(x for x in mcp_server.TOOLS if x["name"] == "followup")
        self.assertIn("question", tool["description"])
        self.assertIn("flush", tool["description"])


if __name__ == "__main__":
    unittest.main()
