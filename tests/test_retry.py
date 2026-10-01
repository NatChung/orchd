import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from orchd import core, mcp_server, store
from orchd.runtime import MODELS, Runtime
from tests.test_orchd import FakeRuntime


class RetryRuntime(FakeRuntime):
    """FakeRuntime plus what retry needs: worktree existence, sleeping, and a worker that may refuse to stop."""

    def __init__(self):
        super().__init__()
        self.missing, self.slept, self.started, self.stop_ignored = set(), [], [], set()
        self.codex_live = set()
        self.next_job = 0

    def exists(self, path):
        return path not in self.missing

    def sleep(self, seconds):
        self.slept.append(seconds)

    def start_worker(self, worktree, sock, brief, model):
        self.next_job += 1
        job, session = f"job{self.next_job}", f"session{self.next_job}"
        self.started.append(("claude", worktree, sock, model))
        self.jobs[job] = {}
        self.brief, self.model = brief, model
        return job, session

    def start_codex_worker(self, worktree, log, prompt, model):
        self.next_job += 1
        self.started.append(("codex", worktree, log, model))
        self.codex_prompt, self.model = prompt, model
        return str(5000 + self.next_job), f"thread-{self.next_job}"

    def stop_worker(self, job):
        self.stopped.append(job)
        if job not in self.stop_ignored:
            self.jobs.pop(job, None)

    def codex_running(self, pid):
        return pid in self.codex_live

    def stop_codex(self, pid):
        self.stopped.append(("codex", pid))
        if pid not in self.stop_ignored:
            self.codex_live.discard(pid)


class RetryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = store.connect(Path(self.tmp.name) / "t.db")
        self.rt = RetryRuntime()

    def tearDown(self):
        self.tmp.cleanup()

    def dispatch(self, model="sonnet"):
        t = core.dispatch(self.con, self.rt, orch_thread="thread-A", repo="demo", title="T",
                          instructions="do it", done_when="tests pass", model=model,
                          model_reason="clear scope", task_type="code")
        self.rt.started.clear()
        return t

    def events(self, task_id, kind):
        return [json.loads(r["body"]) for r in self.con.execute(
            "SELECT body FROM messages WHERE task_id=? AND kind=? ORDER BY id", (task_id, kind))]

    def test_reason_is_required(self):
        t = self.dispatch()
        for reason in (None, "", "   "):
            with self.assertRaises(ValueError):
                core.retry(self.con, self.rt, t["id"], "opus", reason)
        self.assertEqual((self.rt.stopped, self.rt.started), ([], []))

    def test_unknown_model_is_refused(self):
        t = self.dispatch()
        with self.assertRaises(ValueError):
            core.retry(self.con, self.rt, t["id"], "gpt-9", "stuck")
        self.assertEqual(self.rt.stopped, [])

    def test_any_model_change_is_allowed_and_recorded(self):
        for start, target in (("sonnet", "sonnet"), ("sonnet", "opus"), ("opus", "sonnet"),
                              ("sonnet", "sol"), ("sol", "sonnet"), ("sol", "sol")):
            with self.subTest(start=start, target=target):
                t = self.dispatch(start)
                if start == "sol":
                    self.rt.codex_live.add(t["job_id"])
                r = core.retry(self.con, self.rt, t["id"], target, f"{start} to {target}")
                self.assertEqual((r["model"], r["status"], r["worktree"], r["branch"]),
                                 (MODELS[target], "running", t["worktree"], t["branch"]))
                (event,) = self.events(t["id"], "retry")
                self.assertEqual((event["from_model"], event["to_model"], event["reason"]),
                                 (MODELS[start], MODELS[target], f"{start} to {target}"))
                self.assertEqual((event["old_job_id"], event["new_job_id"]), (t["job_id"], r["job_id"]))
                self.assertEqual(self.rt.started[-1][1], t["worktree"])

    def test_new_claude_worker_gets_task_history_reason_and_keep_work_notice(self):
        t = self.dispatch()
        core.progress(self.con, self.rt, t["id"], "half done, tests red")
        core.report(self.con, self.rt, t["id"], "blocked", "cannot fix flake", "commit abc")
        r = core.retry(self.con, self.rt, t["id"], "sonnet", "same model, fresh context")
        self.assertEqual(self.rt.stopped, ["job1"])
        path, session, text = self.rt.sent[-1]
        self.assertEqual((path, session), (r["socket"], r["session_id"]))
        self.assertNotEqual(r["socket"], t["socket"])  # the old socket file is stale
        for needle in (f"[orchd task {t['id']}]", "tests pass", "same model, fresh context",
                       "half done, tests red", "blocked: cannot fix flake", "commit abc",
                       "claude-sonnet-5-5 -> claude-sonnet-5-5", "same model is valid",
                       "never reset, clean, drop", f"ack {t['id']}"):
            self.assertIn(needle, text)
        self.assertTrue(text.rstrip().endswith(f"ack {t['id']}"))
        self.assertIn("Never push directly to the default branch", self.rt.brief)

    def test_cross_vendor_to_codex_uses_a_fresh_log_and_one_prompt(self):
        t = self.dispatch()
        r = core.retry(self.con, self.rt, t["id"], "sol", "second vendor")
        kind, worktree, log, model = self.rt.started[-1]
        self.assertEqual((kind, worktree, model), ("codex", t["worktree"], "gpt-6.1-sol"))
        self.assertNotEqual(log, self.rt.codex_log(t["id"]))  # the old log's thread.started would be read back
        self.assertIn("Never push directly", self.rt.codex_prompt)
        self.assertIn("second vendor", self.rt.codex_prompt)
        self.assertEqual((r["session_id"], r["socket"]), ("thread-2", None))
        core.close(self.con, self.rt, t["id"])  # the stored model decides how close stops it
        self.assertIn(("codex", r["job_id"]), self.rt.stopped)

    def test_claude_retry_of_untrusted_repo_is_refused_before_stopping(self):
        t = self.dispatch("sol")
        self.con.execute("UPDATE tasks SET repo_path='/projects/untrusted' WHERE id=?", (t["id"],))
        with self.assertRaises(ValueError):
            core.retry(self.con, self.rt, t["id"], "opus", "escalate")
        self.assertEqual(self.rt.stopped, [])

    def test_closed_and_missing_worktree_are_refused_before_any_stop(self):
        t = self.dispatch()
        self.rt.missing.add(t["worktree"])
        with self.assertRaisesRegex(ValueError, "no worktree"):
            core.retry(self.con, self.rt, t["id"], "opus", "escalate")
        self.rt.missing.clear()
        store.update_task(self.con, t["id"], status="closed")
        with self.assertRaisesRegex(ValueError, "closed"):
            core.retry(self.con, self.rt, t["id"], "opus", "escalate")
        u = self.dispatch()
        store.update_task(self.con, u["id"], worktree=None)
        with self.assertRaisesRegex(ValueError, "no worktree"):
            core.retry(self.con, self.rt, u["id"], "opus", "escalate")
        self.assertEqual((self.rt.stopped, self.rt.started), ([], []))
        self.assertEqual(self.events(t["id"], "retry") + self.events(u["id"], "retry"), [])

    def test_worker_that_will_not_stop_blocks_the_new_one(self):
        t = self.dispatch()
        self.rt.stop_ignored.add("job1")
        with self.assertRaisesRegex(ValueError, "did not stop"):
            core.retry(self.con, self.rt, t["id"], "opus", "escalate")
        self.assertEqual(self.rt.started, [])
        self.assertEqual(self.rt.stopped, ["job1"])  # stopped once, only its own job
        self.assertEqual(len(self.rt.slept), core.RETRY_STOP_WAIT)  # bounded wait
        after = store.get_task(self.con, t["id"])
        self.assertEqual((after["status"], after["job_id"], after["model"]), ("running", "job1", t["model"]))
        (failed,) = self.events(t["id"], "retry_failed")
        self.assertEqual((failed["stage"], failed["old_job_id"]), ("stop", "job1"))
        self.rt.stop_ignored.clear()  # once it stops, the same retry goes through
        r = core.retry(self.con, self.rt, t["id"], "opus", "escalate")
        self.assertEqual(r["model"], MODELS["opus"])

    def test_unverifiable_claude_job_probe_counts_as_not_stopped(self):
        t = self.dispatch()
        self.rt.live_jobs = lambda: None
        with self.assertRaises(ValueError):
            core.retry(self.con, self.rt, t["id"], "opus", "escalate")
        self.assertEqual(self.rt.started, [])

    def test_running_codex_turn_is_stopped_by_its_own_pid_only(self):
        t = self.dispatch("sol")
        self.rt.codex_live.update({t["job_id"], "999"})
        core.retry(self.con, self.rt, t["id"], "opus", "escalate")
        self.assertEqual(self.rt.stopped, [("codex", t["job_id"])])
        self.assertIn("999", self.rt.codex_live)
        u = self.dispatch("sol")  # between turns there is nothing to stop
        core.retry(self.con, self.rt, u["id"], "sol", "fresh context")
        self.assertNotIn(("codex", u["job_id"]), self.rt.stopped)

    def test_failed_spawn_keeps_worktree_and_can_be_retried(self):
        t = self.dispatch()
        real_start = self.rt.start_worker
        self.rt.start_worker = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no socket"))
        with self.assertRaises(RuntimeError):
            core.retry(self.con, self.rt, t["id"], "opus", "escalate")
        after = store.get_task(self.con, t["id"])
        self.assertEqual((after["status"], after["worktree"], after["branch"]), ("failed", t["worktree"], t["branch"]))
        self.assertIn("retry failed", after["note"])
        self.assertEqual(self.rt.removed, [])
        self.assertEqual(self.events(t["id"], "retry_failed")[0]["stage"], "spawn")
        self.rt.start_worker = real_start
        r = core.retry(self.con, self.rt, t["id"], "opus", "escalate again")
        self.assertEqual(r["status"], "running")
        self.assertEqual(self.rt.removed, [])

    def test_failed_send_keeps_the_new_job_so_it_can_be_stopped(self):
        t = self.dispatch()
        self.rt.send_uds = lambda *a: (_ for _ in ()).throw(OSError("refused"))
        with self.assertRaises(OSError):
            core.retry(self.con, self.rt, t["id"], "opus", "escalate")
        after = store.get_task(self.con, t["id"])
        self.assertEqual((after["status"], after["job_id"]), ("failed", "job2"))
        del self.rt.send_uds
        core.retry(self.con, self.rt, t["id"], "opus", "again")
        self.assertIn("job2", self.rt.stopped)

    def test_each_session_usage_is_recorded_once_across_retries_and_close(self):
        root = Path(self.tmp.name) / "projects" / "-x"
        root.mkdir(parents=True)
        for session, tokens in (("session1", 10), ("session2", 20), ("session3", 30)):
            (root / f"{session}.jsonl").write_text(json.dumps({"type": "assistant", "message": {
                "id": "m", "usage": {"input_tokens": tokens, "output_tokens": 1}}}) + "\n")
        import os
        os.environ["ORCHD_CLAUDE_PROJECTS"] = str(root.parent)
        try:
            t = self.dispatch()
            self.rt.start_worker = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
            with self.assertRaises(RuntimeError):
                core.retry(self.con, self.rt, t["id"], "opus", "first try")
            del self.rt.start_worker
            core.retry(self.con, self.rt, t["id"], "opus", "second try")  # same old session: not counted again
            core.retry(self.con, self.rt, t["id"], "opus", "third")
            core.close(self.con, self.rt, t["id"])
        finally:
            os.environ.pop("ORCHD_CLAUDE_PROJECTS")
        usage = self.events(t["id"], "usage")
        self.assertEqual([u["input_tokens"] for u in usage], [10, 20, 30])
        self.assertEqual([u.get("session_id") for u in usage], ["session1", "session2", None])
        self.assertEqual([u["model"] for u in usage], ["claude-sonnet-5-5", "claude-opus-5-5", "claude-opus-5-5"])
        retries = self.events(t["id"], "retry")
        self.assertEqual([(r["old_job_id"], r["old_session_id"], r["old_model"]) for r in retries],
                         [("job1", "session1", "claude-sonnet-5-5"), ("job2", "session2", "claude-opus-5-5")])

    def test_retry_events_stay_out_of_the_orch_inbox(self):
        t = self.dispatch()
        core.retry(self.con, self.rt, t["id"], "opus", "escalate")
        self.assertEqual(core.inbox(self.con, "thread-A"), [])

    def test_mcp_retry_tool(self):
        tool = next(x for x in mcp_server.TOOLS if x["name"] == "retry")
        self.assertEqual(tool["inputSchema"]["required"], ["task_id", "model", "reason"])
        self.assertEqual(tool["inputSchema"]["properties"]["model"]["enum"], list(MODELS))
        t = self.dispatch()
        out = io.StringIO()
        calls = [{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "retry", "arguments": {
                     "task_id": t["id"], "model": "sol", "reason": "second vendor"}, "_meta": {"threadId": "thread-A"}}},
                 {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "retry", "arguments": {
                     "task_id": t["id"], "model": "opus"}, "_meta": {"threadId": "thread-A"}}}]
        mcp_server.serve(io.StringIO("\n".join(json.dumps(c) for c in calls) + "\n"), out, self.con, self.rt)
        ok, refused = [json.loads(line)["result"] for line in out.getvalue().splitlines()]
        self.assertEqual(json.loads(ok["content"][0]["text"])["model"], "gpt-6.1-sol")
        self.assertTrue(refused["isError"])
        self.assertIn("reason", refused["content"][0]["text"])


class CodexRunningTest(unittest.TestCase):
    def test_reused_or_dead_pid_is_not_a_running_codex(self):
        proc = subprocess.Popen(["sleep", "30"])
        try:
            self.assertFalse(Runtime().codex_running(str(proc.pid)))  # alive, but not codex
        finally:
            proc.kill()
            proc.wait()
        self.assertFalse(Runtime().codex_running(str(proc.pid)))
        self.assertFalse(Runtime().codex_running(None))


class RetryKeepsWorktreeTest(unittest.TestCase):
    """Real git: a retry leaves every kind of local work in the worktree exactly as it was."""

    def git(self, *args, cwd=None):
        return subprocess.run(["git", *args], cwd=cwd or self.wt, check=True, capture_output=True, text=True).stdout

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.repo, self.wt = root / "demo", str(root / "wt")
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main", cwd=self.repo)
        self.git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "base",
                 cwd=self.repo)
        self.git("worktree", "add", "-q", "-b", "orchd/t1", self.wt, cwd=self.repo)
        (Path(self.wt) / "a.txt").write_text("one\n")
        self.git("add", "a.txt")
        self.git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "work")
        (Path(self.wt) / "a.txt").write_text("stashed\n")
        self.git("stash", "push", "-q", "-m", "retry-test-stash")
        (Path(self.wt) / "a.txt").write_text("dirty\n")
        (Path(self.wt) / "new.txt").write_text("untracked\n")
        self.git("branch", "local-only")
        (Path(self.wt) / "sub").mkdir()
        self.git("init", "-q", cwd=Path(self.wt) / "sub")

    def tearDown(self):
        self.tmp.cleanup()

    def snapshot(self):
        return (self.git("status", "--porcelain", "--untracked-files=all"), self.git("rev-parse", "HEAD"),
                self.git("stash", "list"), self.git("branch", "--list"), (Path(self.wt) / "a.txt").read_text(),
                (Path(self.wt) / "new.txt").read_text(), (Path(self.wt) / "sub" / ".git").exists())

    def test_dirty_untracked_stash_branch_and_nested_repo_survive(self):
        commands = []

        class Rt(RetryRuntime):
            exists = staticmethod(Runtime().exists)

            def run(self, cmd, **kw):
                commands.append(cmd)
                raise AssertionError(f"retry ran a command: {cmd}")

        rt = Rt()
        con = store.connect(Path(self.tmp.name) / "t.db")
        store.create_task(con, id="t1", repo="demo", repo_path=str(self.repo), title="T", instructions="i",
                          done_when="d", orch_thread="thread-A", codex_bin="/fake/codex", model=MODELS["sonnet"],
                          base="x", branch="orchd/t1", worktree=self.wt, job_id="job1", session_id="session1",
                          socket="/tmp/x/w.sock", status="blocked")
        before = self.snapshot()
        core.retry(con, rt, "t1", "opus", "escalate")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual((commands, rt.removed), ([], []))
        self.assertEqual(rt.started[0][1], self.wt)


if __name__ == "__main__":
    unittest.main()
