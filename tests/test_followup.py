"""Issue #5 (followup): add an instruction to an open task; same worker, worktree, session; delivery rides on answer."""
import io
import json
import tempfile
import unittest
from pathlib import Path

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

    def test_claude_busy_lock_queues_with_error(self):
        t = self.dispatch()
        orig = store.task_delivery

        def busy(*a, **k):
            raise TimeoutError("lock")
        store.task_delivery = busy
        try:
            out = core.followup(self.con, self.rt, t["id"], "later")
        finally:
            store.task_delivery = orig
        self.assertEqual((out["status"], out["pending"]), ("queued", 1))
        self.assertIn("busy", out["error"])
        (event,) = self.events(t["id"])
        self.assertEqual(event["status"], "queued")

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


if __name__ == "__main__":
    unittest.main()
