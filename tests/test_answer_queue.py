"""Issue #12: answers to a Codex worker that is mid-turn are queued, then flushed into its next turn."""
import errno
import json
import shutil
import socket
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from pathlib import Path

from orchd import core, mcp_server, runtime, store
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
                             done_when="tests pass", model=model, model_reason="r", task_type="code", backend="exec")

    def answers(self, task_id):
        return [r["body"] for r in self.con.execute(
            "SELECT body FROM messages WHERE task_id=? AND kind='answer' ORDER BY id", (task_id,))]

    def assert_visible_pending(self, task_id, n, closed=False):
        # A fresh worker notification lets inbox expose the current task count.
        core.progress(self.con, self.rt, task_id, "safe notification")
        before = [(r["id"], r["body"], r["read_at"]) for r in store.pending_answers(self.con, task_id)]
        with patch.object(store, "pending_answers", side_effect=AssertionError("must not load queued bodies")):
            for _ in range(2):
                tasks = {t["task_id"]: t for t in core.list_open(self.con, self.rt)}
                if closed:
                    self.assertNotIn(task_id, tasks)
                else:
                    self.assertEqual(tasks[task_id]["pending"], n)
                self.assertNotIn("PRIVATE_QUEUE_BODY", json.dumps(tasks))
            messages = core.inbox(self.con, "thread-A")
            self.assertTrue(messages)
            self.assertTrue(all(m["pending"] == n for m in messages if m["task_id"] == task_id))
            self.assertNotIn("PRIVATE_QUEUE_BODY", json.dumps(messages))
        self.assertEqual([(r["id"], r["body"], r["read_at"]) for r in store.pending_answers(self.con, task_id)], before)
        self.assertEqual(store.pending_answer_count(self.con, task_id), n)

    def test_pending_visibility_mixed_fifo_restart_flush_and_legacy(self):
        t = self.dispatch()
        # Old task/message rows need no new schema or stored field.
        store.update_task(self.con, t["id"], model=None)
        self.assert_visible_pending(t["id"], 0)
        store.update_task(self.con, t["id"], model="gpt-6.1-sol")
        self.rt.alive_pids = {"4242"}
        core.answer(self.con, self.rt, t["id"], "PRIVATE_QUEUE_BODY answer")
        core.followup(self.con, self.rt, t["id"], "PRIVATE_QUEUE_BODY followup")
        self.assert_visible_pending(t["id"], 2)
        self.con.close()
        self.con = store.connect(self.db)
        self.assert_visible_pending(t["id"], 2)
        self.rt.alive_pids = set()
        real = self.rt.resume_codex_worker

        def resume(*args):
            # Sending is not a committed receipt yet; an independent reader still sees both.
            other = store.connect(self.db)
            try:
                self.assertEqual(store.pending_answer_count(other, t["id"]), 2)
                self.assertEqual(core.list_open(other, self.rt)[0]["pending"], 2)
            finally:
                other.close()
            return real(*args)

        self.rt.resume_codex_worker = resume
        self.assertEqual(core.answer(self.con, self.rt, t["id"], flush=True)["delivered"], 2)
        self.assert_visible_pending(t["id"], 0)
        sent = self.rt.resumed[-1][1]
        self.assertLess(sent.index("PRIVATE_QUEUE_BODY answer"), sent.index("PRIVATE_QUEUE_BODY followup"))

    def test_pending_visibility_lock_conflict_receipt_failure_and_close(self):
        t = self.dispatch()
        self.rt.alive_pids = {"4242"}
        core.answer(self.con, self.rt, t["id"], "PRIVATE_QUEUE_BODY")
        self.rt.alive_pids = set()
        with patch.object(store, "task_delivery", side_effect=TimeoutError("concurrent flush")):
            with self.assertRaises(TimeoutError):
                core.answer(self.con, self.rt, t["id"], flush=True)
        self.assert_visible_pending(t["id"], 1)
        self.con.execute("CREATE TRIGGER fail_receipt BEFORE UPDATE ON tasks WHEN NEW.job_id='4343' "
                         "BEGIN SELECT RAISE(ABORT,'receipt failure'); END")
        with patch.object(core, "_stop_confirmed", return_value=None):
            self.assertEqual(core.answer(self.con, self.rt, t["id"], flush=True)["status"], "failed")
        self.assert_visible_pending(t["id"], 1)
        self.con.execute("DROP TRIGGER fail_receipt")
        core.close(self.con, self.rt, t["id"])
        self.assert_visible_pending(t["id"], 1, closed=True)

    def test_pending_tool_descriptions_explain_read_only_count(self):
        descriptions = {t["name"]: t["description"] for t in mcp_server.TOOLS}
        for name in ("inbox", "list_open"):
            self.assertIn("pending", descriptions[name])
            self.assertIn("followups", descriptions[name])

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
        self.assertEqual(self.rt.live_jobs(), {"job1": {}})  # the daemon still lists the worker: alive
        with self.assertRaises(ValueError) as caught:
            core.answer(self.con, self.rt, t["id"], "again")
        self.assertNotIn("has ended", str(caught.exception))
        self.assertEqual([r["body"] for r in store.pending_answers(self.con, t["id"])], ["again"])
        self.rt.jobs = {}  # daemon no longer lists it: ended
        with self.assertRaisesRegex(ValueError, "worker has ended"):
            core.answer(self.con, self.rt, t["id"], "once more")
        self.assertEqual([r["body"] for r in store.pending_answers(self.con, t["id"])], ["again", "once more"])

    def test_answer_to_ended_claude_worker_queues_text_and_points_to_retry(self):
        t = self.dispatch(model="sonnet")
        self.rt.jobs = {}

        def gone(*args):
            raise FileNotFoundError(2, "No such file or directory", "/x/worker-abc12345.sock")
        self.rt.send_uds = gone
        with self.assertRaises(ValueError) as caught:
            core.answer(self.con, self.rt, t["id"], "yes please")
        message = str(caught.exception)
        self.assertIn("retry", message)
        self.assertIn("kept as a queued answer", message)
        self.assertNotIn("Errno", message)
        self.assertEqual([r["body"] for r in store.pending_answers(self.con, t["id"])], ["yes please"])
        with self.assertRaisesRegex(ValueError, "earlier queued answer"):  # flush: nothing new, old one still waits
            core.answer(self.con, self.rt, t["id"], flush=True)
        self.assertEqual(len(store.pending_answers(self.con, t["id"])), 1)

    def refuse(self, error):
        def down(*args):
            raise error
        self.rt.send_uds = down

    def test_live_claude_worker_unreachable_keeps_text_with_conditional_retry_advice(self):
        t = self.dispatch(model="sonnet")
        real, sent = self.rt.send_uds, []
        for error in (ConnectionRefusedError("refused"), FileNotFoundError(2, "gone"), ConnectionResetError("reset"),
                      TimeoutError("slow")):
            self.rt.jobs = {"job1": {"state": "idle"}}
            self.refuse(error)
            with self.assertRaises(ValueError) as caught:
                core.answer(self.con, self.rt, t["id"], f"fix {type(error).__name__}")
            message = str(caught.exception)
            self.assertNotIn("has ended", message)
            self.assertIn("answer(flush=true)", message)
            self.assertIn("if you have independently confirmed", message)
        self.assertEqual(len(store.pending_answers(self.con, t["id"])), 4)
        self.rt.send_uds = real
        before = len(self.rt.sent)
        result = core.answer(self.con, self.rt, t["id"], flush=True)
        self.assertEqual(result, dict(status="delivered", delivered=4, pending=0))
        self.assertEqual(len(self.rt.sent) - before, 1)
        self.assertEqual(self.rt.sent[-1][:2], (t["socket"], "session1"))

    def test_unknown_liveness_is_treated_as_alive(self):
        t = self.dispatch(model="sonnet")
        self.rt.live_jobs = lambda: None
        self.refuse(ConnectionRefusedError("refused"))
        with self.assertRaises(ValueError) as caught:
            core.answer(self.con, self.rt, t["id"], "hold this")
        self.assertNotIn("has ended", str(caught.exception))
        self.assertEqual([r["body"] for r in store.pending_answers(self.con, t["id"])], ["hold this"])

    def test_dead_job_state_counts_as_ended(self):
        t = self.dispatch(model="sonnet")
        self.rt.jobs = {"job1": {"state": "failed"}}
        self.refuse(ConnectionRefusedError("refused"))
        with self.assertRaisesRegex(ValueError, "worker has ended"):
            core.answer(self.con, self.rt, t["id"], "x")

    def test_bound_but_not_listening_socket_then_listen_flushes_to_same_worker(self):
        t = self.dispatch(model="sonnet")
        folder = tempfile.mkdtemp(dir="/tmp")
        self.addCleanup(shutil.rmtree, folder, True)
        path = f"{folder}/w.sock"
        server = socket.socket(socket.AF_UNIX)
        self.addCleanup(server.close)
        server.bind(path)  # bound, not yet listening: connect is refused
        store.update_task(self.con, t["id"], socket=path)
        self.rt.send_uds = lambda p, sid, text: runtime.Runtime.send_uds(None, p, sid, text)
        with self.assertRaises(ValueError) as caught:
            core.answer(self.con, self.rt, t["id"], "urgent")
        self.assertNotIn("has ended", str(caught.exception))
        self.assertEqual([r["body"] for r in store.pending_answers(self.con, t["id"])], ["urgent"])
        self.assertIn("if you have independently confirmed", str(caught.exception))
        server.listen(1)
        got = []

        def accept():
            conn, _ = server.accept()
            with conn:
                got.append(conn.makefile().read())
        thread = threading.Thread(target=accept)
        thread.start()
        self.assertEqual(core.answer(self.con, self.rt, t["id"], flush=True), dict(status="delivered", delivered=1, pending=0))
        thread.join(5)
        self.assertEqual(len(got), 1)
        self.assertIn("urgent", got[0])

    def test_failed_claude_interrupt_is_not_queued_for_a_replacement_worker(self):
        t = self.dispatch(model="sonnet")
        before = list(self.rt.sent)
        for jobs in ({"job1": {}}, {}, None):  # alive, ended, unknown
            self.rt.live_jobs = lambda jobs=jobs: jobs
            self.refuse(ConnectionRefusedError("refused"))
            with self.assertRaises(ValueError) as caught:
                core.interrupt(self.con, self.rt, t["id"], "stop, wrong branch")
            self.assertIn("interrupt was not delivered", str(caught.exception))
            self.assertEqual(store.pending_answers(self.con, t["id"]), [])
        self.assertIn("retry", str(caught.exception))
        self.assertEqual(self.rt.sent, before)

    def test_failed_claude_interrupt_leaves_earlier_queued_answers_alone(self):
        t = self.dispatch(model="sonnet")
        store.add_message(self.con, t["id"], store.QUEUED, "earlier")
        self.refuse(ConnectionResetError("reset"))
        with self.assertRaisesRegex(ValueError, "interrupt was not delivered"):
            core.interrupt(self.con, self.rt, t["id"], "now")
        self.assertEqual([r["body"] for r in store.pending_answers(self.con, t["id"])], ["earlier"])

    def test_followup_to_live_unreachable_claude_worker_is_failed_and_queued_once(self):
        t = self.dispatch(model="sonnet")
        self.refuse(ConnectionResetError("reset"))
        with self.assertRaises(ValueError) as caught:
            core.followup(self.con, self.rt, t["id"], "do more")
        self.assertNotIn("has ended", str(caught.exception))
        (event,) = [json.loads(r["body"]) for r in self.con.execute(
            "SELECT body FROM messages WHERE task_id=? AND kind='followup'", (t["id"],))]
        self.assertEqual(event["status"], "failed")
        self.assertIn("if you have independently confirmed", event["error"])
        (queued,) = store.pending_answers(self.con, t["id"])
        self.assertIn("do more", queued["body"])

    def socket_delivery(self, operation, phase):
        """Use an isolated real Unix peer; synchronize close before the real shutdown."""
        t = self.dispatch(model="sonnet")
        folder = tempfile.mkdtemp(dir="/tmp")
        self.addCleanup(shutil.rmtree, folder, True)
        path = f"{folder}/w.sock"
        real_socket = socket.socket
        server = real_socket(socket.AF_UNIX)
        self.addCleanup(server.close)
        server.bind(path)
        server.listen(1)
        server.settimeout(5)
        store.update_task(self.con, t["id"], socket=path)
        got, errors, shutdown_errors = [], [], []
        closed = threading.Event()

        def receive():
            try:
                peer, _ = server.accept()
                with peer:
                    peer.settimeout(5)
                    data = b""
                    while not data.endswith(b"\n"):
                        chunk = peer.recv(65536)
                        if not chunk:
                            break
                        data += chunk
                    got.append(data)
            except Exception as error:
                errors.append(error)
            finally:
                closed.set()

        class Client(real_socket):
            def sendall(client, data):
                if phase == "write":
                    super().sendall(data[:len(data)//2])
                    raise BrokenPipeError("partial frame")
                super().sendall(data)

            def shutdown(client, how):
                if not closed.wait(5):
                    raise AssertionError("peer did not close")
                # On macOS this is the actual OSError(57) regression.
                try:
                    return super().shutdown(how)
                except OSError as error:
                    shutdown_errors.append(error.errno)
                    raise

        thread = threading.Thread(target=receive)
        thread.start()
        self.rt.send_uds = lambda p, sid, text: runtime.Runtime.send_uds(None, p, sid, text)
        try:
            with patch.object(runtime.socket, "socket", Client):
                result = getattr(core, operation)(self.con, self.rt, t["id"], "only once")
        finally:
            thread.join(6)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(got), 1)
        self.assertEqual(store.pending_answers(self.con, t["id"]), [])
        if phase == "write":
            self.assertTrue(got[0])
            self.assertFalse(got[0].endswith(b"\n"))
            self.assertEqual(result["status"], "failed")
            self.assertTrue(result["uncertain"])
            self.assertIn("may have received", result["error"])
            self.assertIn("log", result["error"])
            self.assertNotIn("not delivered", result["error"])
            self.assertEqual(self.answers(t["id"]), [])
        else:
            if sys.platform == "darwin" or shutdown_errors:
                self.assertEqual(shutdown_errors, [errno.ENOTCONN])
            self.assertEqual(len(self.answers(t["id"])), 1)
            self.assertEqual(result, dict(status="delivered", delivered=1, pending=0))
            self.assertEqual(json.loads(got[0])["message"]["content"].count("only once"), 1)
            self.assertEqual(core.answer(self.con, self.rt, t["id"], flush=True)["delivered"], 0)
        if operation == "followup":
            event = json.loads(self.con.execute(
                "SELECT body FROM messages WHERE task_id=? AND kind='followup'", (t["id"],)).fetchone()[0])
            self.assertEqual(event["status"], "uncertain" if phase == "write" else "delivered")

    def test_real_socket_shutdown_after_frame_answer_once(self):
        self.socket_delivery("answer", "shutdown")

    def test_real_socket_shutdown_after_frame_followup_once(self):
        self.socket_delivery("followup", "shutdown")

    def test_real_socket_partial_write_answer_uncertain(self):
        self.socket_delivery("answer", "write")

    def test_real_socket_partial_write_followup_uncertain(self):
        self.socket_delivery("followup", "write")

    def test_real_socket_partial_write_interrupt_uncertain(self):
        self.socket_delivery("interrupt", "write")

    def test_unreachable_probe_failure_or_stale_jobs_gives_conditional_retry(self):
        t = self.dispatch(model="sonnet")
        for probe in (lambda: (_ for _ in ()).throw(OSError("probe failed")),
                      lambda: {"job1": {"state": "idle"}}):
            with self.subTest(probe=probe):
                self.rt.live_jobs = probe
                self.refuse(ConnectionRefusedError("refused"))
                with self.assertRaises(ValueError) as caught:
                    core.answer(self.con, self.rt, t["id"], "keep")
                message = str(caught.exception)
                self.assertIn("answer(flush=true)", message)
                self.assertIn("if you have independently confirmed", message)
                self.assertIn("retry", message)
                self.assertNotIn("do not retry", message)

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


from tests.app_support import AppBase
from unittest.mock import patch
from orchd import core, store

class AppAnswerQueueTest(AppBase):
    def test_busy_answer_is_fifo_and_idle_flush_delivers_one_batch(self):
        self.queue("first");self.queue("second")
        self.assertEqual(self.supervisor.flush()["status"], "queued")
        self.assertEqual(self.supervisor.rpc.calls, [])
        self.idle();result=self.supervisor.flush()
        self.assertEqual(result["delivered"], 2)
        text=self.supervisor.rpc.calls[-1][1]["input"][0]["text"]
        self.assertLess(text.index("first"),text.index("second"))
        self.assertEqual(store.pending_answer_count(self.con,self.id),0)

    def test_receipt_failure_is_uncertain_and_never_resent(self):
        self.queue();self.idle()
        with patch.object(self.supervisor,"receipt",side_effect=OSError("DB unavailable")):
            result=self.supervisor.flush()
        self.assertTrue(result["uncertain"])
        self.assertEqual(store.pending_answer_count(self.con,self.id),1)
        self.assertTrue(self.supervisor.flush()["uncertain"])
        self.assertEqual(len(self.supervisor.rpc.calls),1)
