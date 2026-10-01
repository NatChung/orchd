"""Issue #12: answers to a Codex worker that is mid-turn are queued, then flushed into its next turn."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from orchd import core, mcp_server, store
from tests.test_orchd import FakeRuntime


class AnswerQueueTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "t.db"
        self.con = store.connect(self.db)
        self.rt = FakeRuntime()
        self.ticks = []  # simulated clock: no test really waits
        self.rt.sleep = self.ticks.append

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def dispatch(self, model="sol"):
        return core.dispatch(self.con, self.rt, orch_thread="thread-A", repo="demo", title="T", instructions="do it",
                             done_when="tests pass", model=model, model_reason="r", task_type="code")

    def answers(self, task_id):
        return [r["body"] for r in self.con.execute(
            "SELECT body FROM messages WHERE task_id=? AND kind='answer' ORDER BY id", (task_id,))]

    def test_busy_answers_queue_in_order_and_go_out_once_in_one_turn(self):
        t = self.dispatch()
        core.ask(self.con, self.rt, t["id"], "Which API?")
        self.rt.alive_pids = {"4242"}
        self.assertEqual(core.answer(self.con, self.rt, t["id"], "use v2"), dict(status="queued", delivered=0, pending=1))
        self.assertEqual(core.answer(self.con, self.rt, t["id"], "and keep v1"), dict(status="queued", delivered=0, pending=2))
        self.assertEqual(self.rt.resumed, [])
        self.assertEqual(self.answers(t["id"]), [])  # queued is not delivered
        self.assertEqual(store.get_task(self.con, t["id"])["status"], "question")
        self.assertEqual(core.answer(self.con, self.rt, t["id"], flush=True), dict(status="queued", delivered=0, pending=2))

        self.rt.alive_pids = set()
        self.assertEqual(core.answer(self.con, self.rt, t["id"], "tests too"), dict(status="delivered", delivered=3, pending=0))
        tag = f"[orchd answer {t['id']}]"
        self.assertEqual(self.rt.resumed, [("thread-W", f"{tag}\nuse v2\n\n{tag}\nand keep v1\n\n{tag}\ntests too",
                                            "gpt-6.1-sol")])
        task = store.get_task(self.con, t["id"])
        self.assertEqual((task["job_id"], task["status"]), ("4343", "acked"))
        self.assertEqual(self.answers(t["id"]), ["use v2", "and keep v1", "tests too"])

        self.assertEqual(core.answer(self.con, self.rt, t["id"], flush=True), dict(status="delivered", delivered=0, pending=0))
        self.assertEqual(len(self.rt.resumed), 1)  # nothing is sent twice

    def test_idle_worker_gets_the_answer_at_once_in_the_old_format(self):
        t = self.dispatch()
        core.report(self.con, self.rt, t["id"], "done", "ok", "e")
        self.assertEqual(core.answer(self.con, self.rt, t["id"], "also bump the version"),
                         dict(status="delivered", delivered=1, pending=0))
        self.assertEqual(self.ticks, [])
        self.assertEqual(self.rt.resumed, [("thread-W", f"[orchd answer {t['id']}]\nalso bump the version", "gpt-6.1-sol")])

    def test_busy_wait_is_bounded_to_60_seconds_then_queues(self):
        t = self.dispatch()
        self.rt.alive_pids = {"4242"}
        self.assertEqual(core.answer(self.con, self.rt, t["id"], "x")["status"], "queued")
        self.assertEqual((len(self.ticks), sum(self.ticks)), (120, 60))

    def test_closed_task_keeps_its_queue_and_never_starts_a_turn(self):
        t = self.dispatch()
        self.rt.alive_pids = {"4242"}
        core.answer(self.con, self.rt, t["id"], "late news")
        core.close(self.con, self.rt, t["id"])
        self.rt.alive_pids = set()
        for call in (lambda: core.answer(self.con, self.rt, t["id"], flush=True),
                     lambda: core.answer(self.con, self.rt, t["id"], "more")):
            with self.assertRaisesRegex(ValueError, "closed"):
                call()
        self.assertEqual(self.rt.resumed, [])
        self.assertEqual([r["body"] for r in store.pending_answers(self.con, t["id"])], ["late news"])

    def test_close_while_waiting_is_seen_under_the_lock(self):
        t = self.dispatch()
        self.rt.alive_pids = {"4242"}

        def tick(seconds):  # the Orch closes the task while answer is waiting for the turn to end
            store.update_task(self.con, t["id"], status="closed")
            self.rt.alive_pids = set()
        self.rt.sleep = tick
        with self.assertRaisesRegex(ValueError, "closed"):
            core.answer(self.con, self.rt, t["id"], "x")
        self.assertEqual(self.rt.resumed, [])
        self.assertEqual(len(store.pending_answers(self.con, t["id"])), 1)

    def test_failed_resume_keeps_every_answer_queued_for_a_retry(self):
        t = self.dispatch()
        real = self.rt.resume_codex_worker

        def broken(*args):
            raise OSError("codex not found")
        self.rt.resume_codex_worker = broken
        result = core.answer(self.con, self.rt, t["id"], "go")
        self.assertEqual((result["status"], result["delivered"], result["pending"]), ("failed", 0, 1))
        self.assertIn("codex not found", result["error"])
        self.assertEqual(store.get_task(self.con, t["id"])["job_id"], "4242")
        self.assertEqual(self.answers(t["id"]), [])

        self.rt.resume_codex_worker = real
        self.assertEqual(core.answer(self.con, self.rt, t["id"], flush=True), dict(status="delivered", delivered=1, pending=0))
        self.assertEqual(self.rt.resumed, [("thread-W", f"[orchd answer {t['id']}]\ngo", "gpt-6.1-sol")])

    def test_queue_survives_a_restart(self):
        t = self.dispatch()
        self.rt.alive_pids = {"4242"}
        core.answer(self.con, self.rt, t["id"], "kept")
        self.con.close()
        self.con = store.connect(self.db)
        rt = FakeRuntime()
        rt.sleep = self.ticks.append
        self.assertEqual(core.answer(self.con, rt, t["id"], flush=True)["status"], "delivered")
        self.assertEqual(rt.resumed[0][1], f"[orchd answer {t['id']}]\nkept")

    def test_a_concurrent_flush_does_not_start_a_second_turn(self):
        t = self.dispatch()
        other = store.connect(self.db)
        self.addCleanup(other.close)
        self.rt.alive_pids = {"4242"}
        core.answer(self.con, self.rt, t["id"], "first")
        real_alive, raced = self.rt.pid_alive, []

        def alive(pid):  # between our idle check and our lock, another MCP process flushes and starts a turn
            if not raced:
                raced.append(True)
                self.rt.alive_pids = set()
                core.answer(other, self.rt, t["id"], flush=True)
                self.rt.alive_pids = {"4343"}
                return False
            return real_alive(pid)
        self.rt.alive_pids, self.rt.pid_alive = set(), alive
        # "second" was stored before the race, so the other call sent both; this call must not resend them
        self.assertEqual(core.answer(self.con, self.rt, t["id"], "second"), dict(status="delivered", delivered=0, pending=0))
        tag = f"[orchd answer {t['id']}]"
        self.assertEqual([r[1] for r in self.rt.resumed], [f"{tag}\nfirst\n\n{tag}\nsecond"])
        self.assertEqual(self.answers(t["id"]), ["first", "second"])

        core.answer(other, self.rt, t["id"], "third")  # the new turn is running: this one waits its turn
        self.assertEqual(core.answer(self.con, self.rt, t["id"], flush=True), dict(status="queued", delivered=0, pending=1))

    def test_the_write_lock_excludes_a_second_claimer(self):
        other = sqlite3.connect(self.db, timeout=0.05, isolation_level=None)
        self.addCleanup(other.close)
        with store.immediate(self.con):
            with self.assertRaises(sqlite3.OperationalError):
                other.execute("BEGIN IMMEDIATE")

    def test_queued_answers_stay_out_of_the_orch_inbox(self):
        t = self.dispatch()
        self.rt.alive_pids = {"4242"}
        core.answer(self.con, self.rt, t["id"], "x")
        self.assertEqual([m["kind"] for m in core.inbox(self.con, "thread-A")], [])

    def test_claude_answer_is_unchanged(self):
        t = self.dispatch(model="sonnet")
        self.assertEqual(core.answer(self.con, self.rt, t["id"], "yes"), dict(status="delivered", delivered=1, pending=0))
        self.assertEqual(self.rt.sent[-1], (t["socket"], "session1", f"[orchd answer {t['id']}]\nyes"))
        sent = len(self.rt.sent)
        self.assertEqual(core.answer(self.con, self.rt, t["id"], flush=True), dict(status="delivered", delivered=0, pending=0))
        self.assertEqual(len(self.rt.sent), sent)

        def down(*args):
            raise ConnectionRefusedError("socket gone")
        self.rt.send_uds = down
        with self.assertRaises(ConnectionRefusedError):
            core.answer(self.con, self.rt, t["id"], "again")

    def test_text_or_flush_is_required(self):
        t = self.dispatch()
        with self.assertRaisesRegex(ValueError, "flush"):
            core.answer(self.con, self.rt, t["id"])

    def test_mcp_answer_returns_the_delivery_status(self):
        t = self.dispatch()
        self.rt.alive_pids = {"4242"}

        def call(args):
            reply = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                       "params": {"name": "answer", "arguments": args}}, self.con, self.rt)
            return json.loads(reply["result"]["content"][0]["text"])
        self.assertEqual(call({"task_id": t["id"], "text": "x"})["status"], "queued")
        self.rt.alive_pids = set()
        self.assertEqual(call({"task_id": t["id"], "flush": True}), dict(status="delivered", delivered=1, pending=0))
        schema = next(tool for tool in mcp_server.TOOLS if tool["name"] == "answer")["inputSchema"]
        self.assertEqual(schema["required"], ["task_id"])


if __name__ == "__main__":
    unittest.main()
