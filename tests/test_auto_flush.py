"""Issue #12 (2026-10-04): a Codex turn's shell flushes the queue when codex exits; interrupt stops a turn."""
import json
import os
import stat
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from orchd import cli, core, mcp_server, store
from orchd.runtime import Runtime
from tests.test_orchd import FakeRuntime


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = store.connect(Path(self.tmp.name) / "t.db")
        self.rt = FakeRuntime()
        self.rt.sleep = lambda s: None
        t = core.dispatch(self.con, self.rt, orch_thread="thread-A", repo="demo", title="T", instructions="do it",
                          done_when="x", model="sol", model_reason="r", task_type="code")
        self.id = t["id"]  # job 4242 (the first turn) is alive
        self.rt.alive_pids = {"4242"}

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def queue(self, text):
        store.add_message(self.con, self.id, store.QUEUED, text)


class AutoFlushTest(Base):
    def test_turn_end_sends_queue_once_in_fifo_order(self):
        self.queue("first")
        self.queue("second")
        result = core.auto_flush(self.con, self.rt, self.id, 4242)  # the wrapper's own pid is still alive
        self.assertEqual((result["status"], result["delivered"], result["pending"]), ("delivered", 2, 0))
        (thread, text, _), = self.rt.resumed
        self.assertLess(text.index("first"), text.index("second"))
        self.assertEqual(store.get_task(self.con, self.id)["job_id"], "4343")
        again = core.auto_flush(self.con, self.rt, self.id, 4242)  # same turn firing twice sends nothing
        self.assertEqual(again["status"], "skipped")
        self.assertEqual(len(self.rt.resumed), 1)

    def test_manual_flush_first_leaves_auto_flush_nothing_to_send(self):
        self.queue("a")
        self.rt.alive_pids = set()
        core.answer(self.con, self.rt, self.id, flush=True)
        self.assertEqual(core.auto_flush(self.con, self.rt, self.id, 4242)["status"], "skipped")
        self.assertEqual(len(self.rt.resumed), 1)

    def test_empty_queue_and_stale_turn_do_nothing(self):
        self.assertEqual(core.auto_flush(self.con, self.rt, self.id, 4242)["reason"], "nothing queued")
        self.queue("a")
        self.assertEqual(core.auto_flush(self.con, self.rt, self.id, 9999)["reason"], "superseded")
        self.assertEqual(self.rt.resumed, [])

    def test_stale_turn_is_rechecked_under_the_delivery_lock(self):
        """Precheck passes (job 4242), then a newer turn replaces the job before _answer takes the lock."""
        self.queue("a")
        original = core._answer

        def replaced_then_answer(*args, **kwargs):
            store.update_task(self.con, self.id, job_id="9999")  # a newer turn that has already exited
            return original(*args, **kwargs)
        with patch.object(core, "_answer", side_effect=replaced_then_answer):
            result = core.auto_flush(self.con, self.rt, self.id, 4242)
        self.assertEqual((result["status"], result["reason"]), ("skipped", "superseded"))
        self.assertEqual(self.rt.resumed, [])
        self.assertEqual(store.pending_answer_count(self.con, self.id), 1)

    def test_closed_task_is_never_flushed(self):
        self.queue("a")
        store.update_task(self.con, self.id, status="closed")
        self.assertEqual(core.auto_flush(self.con, self.rt, self.id, 4242), dict(status="skipped", reason="closed"))
        self.assertEqual(self.rt.resumed, [])
        self.assertEqual(store.pending_answer_count(self.con, self.id), 1)

    def test_failure_stays_queued_and_reaches_the_inbox(self):
        self.queue("a")

        def broken(*args):
            raise OSError("codex not found")
        self.rt.resume_codex_worker = broken
        result = core.auto_flush(self.con, self.rt, self.id, 4242)
        self.assertEqual(result["status"], "failed")
        self.assertIn("codex not found", result["error"])
        self.assertEqual(store.pending_answer_count(self.con, self.id), 1)
        (message,) = [m for m in core.inbox(self.con, "thread-A") if "auto-flush failed" in m["body"]]
        self.assertEqual(message["pending"], 1)
        self.assertIn("codex not found", message["body"])
        self.assertEqual(core.list_open(self.con, self.rt)[0]["pending"], 1)

    def test_unexpected_exception_is_recorded_not_raised(self):
        self.queue("a")
        with patch.object(core, "_answer", side_effect=RuntimeError("db exploded")):
            result = core.auto_flush(self.con, self.rt, self.id, 4242)
        self.assertEqual(result["status"], "failed")
        self.assertTrue([m for m in core.inbox(self.con, "thread-A") if "db exploded" in m["body"]])

    def test_cli_flush_runs_core_and_exit_code_follows_failure(self):
        self.queue("a")
        with patch.object(cli.store, "connect", return_value=self.con), patch.object(cli, "Runtime", return_value=self.rt):
            self.assertEqual(cli.main(["flush", self.id, "--after-pid", "4242"]), 0)
        self.assertEqual(len(self.rt.resumed), 1)

    def test_answer_during_turn_is_sent_by_auto_flush(self):
        self.rt.sleep = lambda s: None
        out = core.answer(self.con, self.rt, self.id, "fix this")  # turn still alive: queued after the wait
        self.assertEqual(out["status"], "queued")
        self.assertEqual(core.auto_flush(self.con, self.rt, self.id, 4242)["delivered"], 1)


class InterruptTest(Base):
    def setUp(self):
        super().setUp()

        def stop(kind, job, marks=(), wait=10.0):
            self.rt.stopped.append((kind, job, tuple(marks)))
            self.rt.alive_pids.discard(job)
        self.rt.stop_task_worker = stop

    def test_interrupt_stops_running_turn_then_resumes_with_text(self):
        self.queue("earlier")
        result = core.interrupt(self.con, self.rt, self.id, "stop, use main")
        self.assertTrue(result["interrupted"])
        self.assertEqual((result["status"], result["delivered"], result["pending"]), ("delivered", 2, 0))
        self.assertEqual(self.rt.stopped[0][:2], ("codex", "4242"))
        (_, text, _), = self.rt.resumed
        self.assertLess(text.index("earlier"), text.index("stop, use main"))
        self.assertIn("[interrupt]", text)
        self.assertEqual(store.get_task(self.con, self.id)["job_id"], "4343")

    def test_interrupt_between_turns_is_a_plain_resume(self):
        self.rt.alive_pids = set()
        result = core.interrupt(self.con, self.rt, self.id, "now this")
        self.assertFalse(result["interrupted"])
        self.assertEqual(result["status"], "delivered")

    def test_unconfirmed_stop_resumes_nothing_and_keeps_text(self):
        def stuck(kind, job, marks=(), wait=10.0):
            raise RuntimeError("still running")
        self.rt.stop_task_worker = stuck
        result = core.interrupt(self.con, self.rt, self.id, "now this")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.rt.resumed, [])
        self.assertEqual(store.pending_answer_count(self.con, self.id), 1)

    def test_resume_failure_keeps_text_queued(self):
        def broken(*args):
            raise OSError("codex not found")
        self.rt.resume_codex_worker = broken
        result = core.interrupt(self.con, self.rt, self.id, "now this")
        self.assertEqual((result["status"], result["pending"]), ("failed", 1))

    def test_auto_flush_racing_the_stop_cannot_resume_between_stop_and_resume(self):
        """The old turn exits just as interrupt stops it and its auto flush starts. The flush must wait for the
        task lock, then see the newer job and skip: one resume, a live worker, nothing left queued."""
        self.rt.alive_pids = {"4242"}
        flush = {}
        resume = self.rt.resume_codex_worker

        def resume_alive(*args):
            job = resume(*args)
            self.rt.alive_pids.add(job)
            return job
        self.rt.resume_codex_worker = resume_alive

        def stop(kind, job, marks=(), wait=10.0):
            self.rt.alive_pids.discard("4242")  # codex exited by itself, its wrapper's flush starts now

            def run():
                con = store.connect(Path(self.tmp.name) / "t.db")
                try:
                    flush["result"] = core.auto_flush(con, self.rt, self.id, 4242)
                finally:
                    con.close()
            flush["thread"] = threading.Thread(target=run)
            flush["thread"].start()
            flush["thread"].join(0.5)  # unfixed code lets it run to the end here; fixed code blocks it on the lock
            self.rt.alive_pids.clear()  # #48 cleanup of the worktree's processes
        self.rt.stop_task_worker = stop
        result = core.interrupt(self.con, self.rt, self.id, "urgent correction")
        flush["thread"].join(10)
        self.assertEqual((result["status"], result["delivered"], result["pending"]), ("delivered", 1, 0))
        self.assertEqual(len(self.rt.resumed), 1)
        self.assertIn("urgent correction", self.rt.resumed[0][1])
        self.assertEqual(self.rt.alive_pids, {"4343"})
        self.assertEqual(flush["result"]["status"], "skipped")
        self.assertEqual(flush["result"]["reason"], "superseded")
        self.assertEqual(store.get_task(self.con, self.id)["job_id"], "4343")

    def test_busy_task_lock_queues_the_correction_and_stops_nothing(self):
        with patch.object(store, "task_delivery", side_effect=TimeoutError("busy")):
            result = core.interrupt(self.con, self.rt, self.id, "now this")
        self.assertEqual((result["status"], result["pending"], result["interrupted"]), ("queued", 1, False))
        self.assertEqual(self.rt.stopped, [])
        self.assertEqual(self.rt.resumed, [])

    def test_closed_and_empty_are_refused(self):
        with self.assertRaises(ValueError):
            core.interrupt(self.con, self.rt, self.id, "  ")
        store.update_task(self.con, self.id, status="closed")
        with self.assertRaises(ValueError):
            core.interrupt(self.con, self.rt, self.id, "x")

    def test_claude_worker_gets_a_socket_message_and_nothing_stops(self):
        t = core.dispatch(self.con, self.rt, orch_thread="thread-A", repo="demo", title="C", instructions="i",
                          done_when="x", model="sonnet", model_reason="r", task_type="code")
        result = core.interrupt(self.con, self.rt, t["id"], "change course")
        self.assertFalse(result["interrupted"])
        self.assertIn("socket", result["note"])
        self.assertEqual(self.rt.stopped, [])
        self.assertIn("change course", self.rt.sent[-1][2])

    def test_mcp_tool_listed_and_warns(self):
        tool = next(x for x in mcp_server.TOOLS if x["name"] == "interrupt")
        self.assertIn("half-done", tool["description"])
        self.assertIn("EMERGENCY", tool["description"])
        out = mcp_server.call("interrupt", {"task_id": self.id, "text": "go"}, "thread-A", self.con, self.rt)
        self.assertTrue(out["interrupted"])


class BriefTest(unittest.TestCase):
    def test_codex_brief_has_short_turn_rules_claude_brief_does_not(self):
        codex, claude = core.worker_brief("orchd", "codex"), core.worker_brief("orchd", "claude")
        self.assertIn("Keep every turn short", codex)
        self.assertIn("background", codex)
        self.assertIn("opening a PR, merging", codex)
        self.assertNotIn("Keep every turn short", claude)


class WrapperCleanupTest(unittest.TestCase):
    """macOS psutil cannot read the sh wrapper's own ORCHD_WORKTREE (only its children's): close and interrupt must
    still stop the wrapper and the codex child, through the args match and killpg, not through the env mark."""

    def _spawn(self, tmp):
        codex, orchd, ready, trace = tmp / "codex", tmp / "orchd", tmp / "ready", tmp / "trace"
        codex.write_text('#!/bin/sh\ntouch "$READY"\nsleep 60\n')
        orchd.write_text('#!/bin/sh\necho flush >> "$TRACE"\n')
        for f in (codex, orchd):
            f.chmod(f.stat().st_mode | stat.S_IEXEC)
        worktree = tmp / "wt"
        log = worktree / "orchd-wrap01" / "codex.jsonl"
        log.parent.mkdir(parents=True)
        rt = Runtime()
        rt.codex = str(codex)
        env = {"ORCHD_EXECUTABLE": str(orchd), "ORCHD_HOME": str(tmp / "home"), "READY": str(ready),
               "TRACE": str(trace)}
        with patch.dict(os.environ, env):
            pid = rt.resume_codex_worker(str(worktree), str(log), "th-wrap", "hello", "gpt-6.1-sol")
        for _ in range(200):
            if ready.exists():
                break
            time.sleep(0.05)
        self.assertTrue(ready.exists())
        return rt, pid, worktree, trace

    def _assert_all_gone(self, rt, pid, worktree, trace):
        from orchd.processes import task_processes
        self.assertFalse(rt.pid_alive(pid))
        self.assertEqual(task_processes(str(worktree)), {})
        time.sleep(0.3)
        self.assertFalse(trace.exists(), "the killed wrapper must not run its flush")

    def test_interrupt_stop_kills_wrapper_and_codex_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            rt, pid, worktree, trace = self._spawn(Path(tmp))
            try:
                rt.stop_task_worker("codex", pid, (str(worktree), "th-wrap"), wait=3)
                self._assert_all_gone(rt, pid, worktree, trace)
            finally:
                try:
                    os.killpg(int(pid), 9)
                except OSError:
                    pass

    def test_close_kills_wrapper_and_codex_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            rt, pid, worktree, trace = self._spawn(tmp)
            con = store.connect(tmp / "t.db")
            try:
                store.create_task(con, id="wrap01", repo="demo", repo_path=str(worktree), title="T", instructions="i",
                                  done_when="d", orch_thread="thread-A", codex_bin="x", model="gpt-6.1-sol",
                                  worktree=str(worktree), job_id=pid, session_id="th-wrap", status="running")
                with patch.object(rt, "worktree_state", return_value=(False, "kept")), \
                        patch.object(rt, "codex_usage", return_value=None):
                    core.close(con, rt, "wrap01")
                self.assertEqual(store.get_task(con, "wrap01")["status"], "closed")
                self._assert_all_gone(rt, pid, worktree, trace)
            finally:
                con.close()
                try:
                    os.killpg(int(pid), 9)
                except OSError:
                    pass


class TurnWrapperTest(unittest.TestCase):
    """The real shell wrapper, with stub codex and stub orchd executables."""

    def test_flush_runs_after_codex_exits_with_the_wrappers_pid(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            codex, orchd = tmp / "codex", tmp / "orchd"
            codex.write_text('#!/bin/sh\necho "codex $@" >> "$TRACE"\nexit 3\n')  # a failing codex still flushes
            orchd.write_text('#!/bin/sh\necho "orchd $@" >> "$TRACE"\n')
            for f in (codex, orchd):
                f.chmod(f.stat().st_mode | stat.S_IEXEC)
            log = tmp / "orchd-abc123" / "codex.jsonl"
            log.parent.mkdir()
            trace = tmp / "trace"
            rt = Runtime()
            rt.codex = str(codex)
            with patch.dict(os.environ, {"ORCHD_EXECUTABLE": str(orchd), "TRACE": str(trace)}):
                pid = int(rt.resume_codex_worker(str(tmp), str(log), "th-1", "hello world", "gpt-6.1-sol"))
                for _ in range(100):
                    if trace.exists() and "orchd" in trace.read_text():
                        break
                    time.sleep(0.05)
            lines = trace.read_text().splitlines()
            self.assertEqual(lines[0], "codex exec resume --json --dangerously-bypass-approvals-and-sandbox "
                                       "-m gpt-6.1-sol th-1 hello world")
            self.assertEqual(lines[1], f"orchd flush abc123 --after-pid {pid}")

    def test_log_outside_a_task_directory_is_not_wrapped(self):
        self.assertEqual(Runtime().codex_turn(["codex"], "/l/codex.jsonl"), ["codex"])


if __name__ == "__main__":
    unittest.main()
