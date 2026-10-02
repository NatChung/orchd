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


class FollowupReceiptBoundaryTest(unittest.TestCase):
    """PR #32 review 94c5cd97 v2: committed acceptance identity, every after-acceptance lock timeout, a send whose
    receipt fails, and a Codex resume spawned with no SQL write lock held (shared with plain answer flush)."""

    setUp, tearDown, done_task, events = (FollowupAcceptanceTest.setUp, FollowupAcceptanceTest.tearDown,
                                          FollowupAcceptanceTest.done_task, FollowupAcceptanceTest.events)
    holder, stage = FollowupAcceptanceTest.holder, FollowupAcceptanceTest.stage

    def peer(self, code, *args):
        """Run `code` in another process on the same database (a real second connection)."""
        return subprocess.check_output([sys.executable, "-c", "import sys\nfrom orchd import store\n"
                                        "c=store.connect(sys.argv[1])\n" + code + "\nc.close()\n",
                                        str(self.db), *args], cwd=REPO_ROOT, text=True).strip()

    def stops(self, confirmed=True):
        stopped = []

        def stop(kind, job, marks=(), wait=10.0):
            stopped.append((kind, job, tuple(marks)))
            if not confirmed:
                raise RuntimeError(f"pid {job} still running")
        self.rt.stop_task_worker = stop
        return stopped

    # P1: a rolled-back acceptance id is never used
    def test_rolled_back_acceptance_id_reused_by_another_process_report_is_not_overwritten(self):
        t = self.done_task("sol")
        self.con.execute("CREATE TRIGGER fail_queue BEFORE INSERT ON messages WHEN NEW.kind='answer_queued' "
                         "BEGIN SELECT RAISE(ABORT,'injected queue failure'); END")
        real_immediate, real_add, staged, reused = store.immediate, store.add_message, [], []

        def add(c, task_id, kind, body, evidence=None):
            row_id = real_add(c, task_id, kind, body, evidence)
            if kind == "followup":
                staged.append(row_id)
            return row_id

        @contextlib.contextmanager
        def rollback_then_competing_report(c):
            try:
                with real_immediate(c):
                    yield
            except sqlite3.IntegrityError:  # rolled back: another process now inserts a report
                reused.append(int(self.peer("print(store.add_message(c,sys.argv[2],'report','done: competing',"
                                            "'their evidence'))", t["id"])))
                raise
        with patch.object(store, "immediate", rollback_then_competing_report), patch.object(store, "add_message", add):
            with self.assertRaisesRegex(sqlite3.IntegrityError, "injected queue failure"):
                core.followup(self.con, self.rt, t["id"], "rejected queue insert")
        self.assertEqual(staged, reused)  # the id really was reused
        row = self.con.execute("SELECT task_id,kind,body,evidence FROM messages WHERE id=?", (reused[0],)).fetchone()
        self.assertEqual(tuple(row), (t["id"], "report", "done: competing", "their evidence"))
        self.assertEqual((self.events(t), store.pending_answers(self.con, t["id"]), self.rt.resumed), ([], [], []))
        (first,) = [r for r in self.con.execute("SELECT body,evidence FROM messages WHERE task_id=? AND kind='report' "
                                                "ORDER BY id", (t["id"],))][:1]
        self.assertEqual(tuple(first), ("done: prior result", "prior evidence"))

    def test_record_only_writes_the_row_with_this_acceptance_token(self):
        t = self.done_task()
        core.followup(self.con, self.rt, t["id"], "one")
        core.followup(self.con, self.rt, t["id"], "one")
        first, second = self.events(t)
        self.assertNotEqual(first["token"], second["token"])
        self.assertEqual((first["status"], second["status"]), ("delivered", "delivered"))
        # Another writer replaced the event body after acceptance: the result update matches nothing.
        self.con.execute("CREATE TRIGGER swap AFTER INSERT ON messages WHEN NEW.kind='followup' BEGIN "
                         "UPDATE messages SET body='{\"other\": 1}' WHERE id=NEW.id; END")
        out = core.followup(self.con, self.rt, t["id"], "two")
        self.assertEqual(out["status"], "delivered")
        self.assertIn("not found", out["record_error"])
        self.assertEqual(self.events(t)[-1], {"other": 1})

    # P2: every lock timeout after acceptance is the accepted/queued receipt
    def test_accepted_codex_switched_to_claude_whose_lock_is_busy_returns_queued_then_flush_delivers_once(self):
        t = self.done_task("sol")
        real, calls = store.task_delivery, []
        with contextlib.ExitStack() as stack:
            def acquire(c, ids, timeout=65):
                calls.append(1)
                if len(calls) == 2:  # an independent retry switched the task to a Claude worker after acceptance
                    store.update_task(c, t["id"], model="claude-sonnet-5-5", socket="/fake/c.sock", session_id="s2")
                if len(calls) == 3:  # and another process holds the real lock when we take it again
                    self.stage(stack.enter_context(self.holder(t)), "hold")
                return real(c, ids, timeout=0.05)
            with patch.object(store, "task_delivery", acquire):
                out = core.followup(self.con, self.rt, t["id"], "accepted before the switch")
        self.assertEqual((out["status"], out["delivered"], out["pending"]), ("queued", 0, 1))
        self.assertIn("busy", out["error"])
        self.assertEqual((self.events(t)[0]["status"], self.rt.sent), ("queued", []))
        self.assertEqual(core.answer(self.con, self.rt, t["id"], flush=True), dict(status="delivered", delivered=1, pending=0))
        self.assertEqual(len(self.rt.sent), 1)
        self.assertTrue(self.rt.sent[0][2].endswith("accepted before the switch"))
        self.assertEqual(core.answer(self.con, self.rt, t["id"], flush=True)["delivered"], 0)

    def test_plain_answer_switched_to_claude_with_busy_lock_still_raises(self):
        t = self.done_task("sol")
        real, calls = store.task_delivery, []

        def acquire(c, ids, timeout=65):
            calls.append(1)
            if len(calls) == 1:
                store.update_task(c, t["id"], model="claude-sonnet-5-5", socket="/fake/c.sock", session_id="s2")
            if len(calls) == 2:
                raise TimeoutError("busy")
            return real(c, ids, timeout=timeout)
        with patch.object(store, "task_delivery", acquire):
            with self.assertRaises(TimeoutError):
                core.answer(self.con, self.rt, t["id"], "plain")
        self.assertEqual(len(store.pending_answers(self.con, t["id"])), 1)  # stored before any attempt, as before

    # P3: a send that succeeded is never reported as not sent
    def test_claude_send_ok_receipt_failure_is_delivered_with_record_error_and_no_resend(self):
        t = self.done_task()
        self.con.execute("CREATE TRIGGER fail_receipt BEFORE UPDATE ON tasks WHEN NEW.status='acked' "
                         "BEGIN SELECT RAISE(ABORT,'injected receipt failure'); END")
        out = core.followup(self.con, self.rt, t["id"], "sent once")
        self.assertEqual((out["status"], out["delivered"], out["pending"]), ("delivered", 1, 0))
        self.assertIn("injected receipt failure", out["record_error"])
        self.assertEqual(len(self.rt.sent), 1)
        (event,) = self.events(t)
        self.assertEqual(event["status"], "delivered")
        self.assertIn("injected receipt failure", event["record_error"])
        self.assertEqual(store.get_task(self.con, t["id"])["status"], "done")  # the receipt really was not written
        self.assertEqual([r[0] for r in self.con.execute("SELECT evidence FROM messages WHERE task_id=? AND "
                                                         "kind='report'", (t["id"],))], ["prior evidence"])
        self.assertEqual(core.answer(self.con, self.rt, t["id"], flush=True)["delivered"], 0)  # nothing to resend
        self.assertEqual(len(self.rt.sent), 1)

    def test_receipt_failure_names_queued_rows_that_went_out(self):
        t = self.done_task("sol")
        self.rt.alive_pids = {"4242"}
        core.answer(self.con, self.rt, t["id"], "queued for codex")
        (row,) = store.pending_answers(self.con, t["id"])
        store.update_task(self.con, t["id"], model="claude-sonnet-5-5", socket="/fake/c.sock", session_id="s2")
        self.con.execute("CREATE TRIGGER fail_receipt BEFORE UPDATE ON tasks WHEN NEW.status='acked' "
                         "BEGIN SELECT RAISE(ABORT,'injected receipt failure'); END")
        out = core.followup(self.con, self.rt, t["id"], "and this")
        self.assertEqual((out["status"], out["delivered"], out["pending"]), ("delivered", 2, 1))
        self.assertIn(f"[{row['id']}]", out["record_error"])
        self.assertIn("do not flush", out["record_error"])

    def test_plain_answer_receipt_failure_still_raises(self):
        t = self.done_task()
        self.con.execute("CREATE TRIGGER fail_receipt BEFORE UPDATE ON tasks WHEN NEW.status='acked' "
                         "BEGIN SELECT RAISE(ABORT,'injected receipt failure'); END")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected receipt failure"):
            core.answer(self.con, self.rt, t["id"], "plain")
        self.assertEqual(len(self.rt.sent), 1)

    # P4: no SQL write transaction across the Codex resume (followup and plain answer share the flush)
    def test_codex_resume_holds_no_sql_write_lock_another_process_can_write(self):
        for call in ("followup", "answer"):
            with self.subTest(call=call):
                t = self.done_task("sol")
                real, seen = self.rt.resume_codex_worker, []

                def resume(*args):
                    seen.append(self.con.in_transaction)
                    # a real second process takes SQLite's write lock while the resume is in flight
                    seen.append(self.peer("c.execute('PRAGMA busy_timeout=200')\nwith store.immediate(c):\n"
                                          " store.add_message(c,sys.argv[2],'progress','peer wrote')\nprint('ok')",
                                          t["id"]))
                    return real(*args)
                with patch.object(self.rt, "resume_codex_worker", resume):
                    out = (core.followup(self.con, self.rt, t["id"], "go") if call == "followup"
                           else core.answer(self.con, self.rt, t["id"], "go"))
                self.assertEqual(out, dict(status="delivered", delivered=1, pending=0))
                self.assertEqual(seen, [False, "ok"])
                task = store.get_task(self.con, t["id"])
                self.assertEqual((task["job_id"], task["status"], task["session_id"]), ("4343", "acked", "thread-W"))

    def test_resumed_worker_report_before_the_receipt_commit_keeps_its_status_and_report(self):
        t = self.done_task("sol")
        store.update_task(self.con, t["id"], status="acked")  # the earlier report was read; worker idle
        real = self.rt.resume_codex_worker

        def resume(*args):  # the resumed worker reports from its own process before our receipt commits
            job = real(*args)
            # report's own writes (its Orch wake waits for the task lock this flush holds, so it is left out)
            self.peer("store.add_message(c,sys.argv[2],'report','done: second pass','new evidence')\n"
                      "store.update_task(c,sys.argv[2],status='done')", t["id"])
            return job
        with patch.object(self.rt, "resume_codex_worker", resume):
            self.assertEqual(core.followup(self.con, self.rt, t["id"], "more")["status"], "delivered")
        task = store.get_task(self.con, t["id"])
        self.assertEqual((task["status"], task["job_id"]), ("done", "4343"))
        self.assertEqual([tuple(r) for r in self.con.execute(
            "SELECT body,evidence FROM messages WHERE task_id=? AND kind='report' ORDER BY id", (t["id"],))],
            [("done: prior result", "prior evidence"), ("done: second pass", "new evidence")])

    def test_resume_commit_failure_stops_that_worker_and_keeps_mixed_queue_fifo(self):
        for call in ("followup", "answer"):
            with self.subTest(call=call):
                t = self.done_task("sol")
                self.rt.alive_pids = {"4242"}
                core.answer(self.con, self.rt, t["id"], "plain first")
                core.followup(self.con, self.rt, t["id"], "followup second")
                self.rt.alive_pids, self.rt.resumed = set(), []
                stopped = self.stops()
                self.con.execute("CREATE TRIGGER fail_commit BEFORE UPDATE ON tasks WHEN NEW.job_id='4343' "
                                 "BEGIN SELECT RAISE(ABORT,'injected commit failure'); END")
                out = (core.followup(self.con, self.rt, t["id"], "third") if call == "followup"
                       else core.answer(self.con, self.rt, t["id"], flush=True))
                n = 3 if call == "followup" else 2
                self.assertEqual((out["status"], out["delivered"], out["pending"], out["uncertain"]), ("failed", 0, n, True))
                self.assertIn("injected commit failure", out["error"])
                self.assertIn("4343 had started", out["error"])
                self.assertIn("was stopped", out["error"])
                self.assertEqual(stopped, [("codex", "4343", (t["worktree"], "thread-W"))])
                task = store.get_task(self.con, t["id"])
                self.assertEqual((task["job_id"], task["status"]), (t["job_id"], "done"))  # no orphan job recorded
                self.assertEqual(self.con.execute("SELECT COUNT(*) FROM messages WHERE task_id=? AND kind='answer'",
                                                  (t["id"],)).fetchone()[0], 0)
                self.con.execute("DROP TRIGGER fail_commit")
                self.assertEqual(core.answer(self.con, self.rt, t["id"], flush=True)["delivered"], n)
                message = self.rt.resumed[-1][1]
                self.assertLess(message.index("plain first"), message.index("followup second"))
                self.assertEqual(core.answer(self.con, self.rt, t["id"], flush=True)["delivered"], 0)
                if call == "followup":
                    self.assertEqual([e["status"] for e in self.events(t)], ["queued", "failed"])

    def test_resume_commit_failure_with_unconfirmed_stop_keeps_the_pid_so_no_second_resume(self):
        t = self.done_task("sol")
        stopped = self.stops(confirmed=False)
        self.con.execute("CREATE TRIGGER fail_commit BEFORE UPDATE ON tasks WHEN NEW.status='acked' "
                         "BEGIN SELECT RAISE(ABORT,'injected commit failure'); END")
        out = core.followup(self.con, self.rt, t["id"], "maybe running")
        self.assertEqual((out["status"], out["pending"], out["uncertain"]), ("failed", 1, True))
        self.assertIn("4343", out["error"])
        self.assertIn("MAY STILL BE RUNNING", out["error"])
        self.assertEqual(len(stopped), 1)
        self.assertEqual(store.get_task(self.con, t["id"])["job_id"], "4343")
        self.rt.alive_pids = {"4343"}
        self.assertEqual(core.answer(self.con, self.rt, t["id"], flush=True)["status"], "queued")
        self.assertEqual(len(self.rt.resumed), 1)  # never a second actor on the thread

    def test_resume_superseded_by_another_writer_is_stopped(self):
        for change in ("closed", "read"):
            with self.subTest(change=change):
                t = self.done_task("sol")
                stopped = self.stops()
                real = self.rt.resume_codex_worker

                def resume(*args):  # a writer that did not take the task lock (an older orchd) lands meanwhile
                    if change == "closed":
                        self.peer("c.execute(\"UPDATE tasks SET status='closed' WHERE id=?\",(sys.argv[2],))", t["id"])
                    else:
                        self.peer("c.execute(\"UPDATE messages SET read_at=1 WHERE task_id=? AND kind='answer_queued'\","
                                  "(sys.argv[2],))", t["id"])
                    return real(*args)
                with patch.object(self.rt, "resume_codex_worker", resume):
                    out = core.answer(self.con, self.rt, t["id"], "x")
                self.assertEqual((out["status"], out["uncertain"]), ("failed", True))
                self.assertIn("_Superseded", out["error"])
                self.assertEqual(stopped, [("codex", "4343", (t["worktree"], "thread-W"))])
                self.assertNotEqual(store.get_task(self.con, t["id"])["job_id"], "4343")

    def test_spawn_failure_receipt_unchanged_and_nothing_stopped(self):
        t = self.done_task("sol")
        stopped = self.stops()

        def fail(*a):
            raise RuntimeError("codex down")
        self.rt.resume_codex_worker = fail
        out = core.answer(self.con, self.rt, t["id"], "x")
        self.assertEqual(out, dict(status="failed", delivered=0, pending=1, error="RuntimeError: codex down"))
        self.assertEqual(stopped, [])


if __name__ == "__main__":
    unittest.main()
