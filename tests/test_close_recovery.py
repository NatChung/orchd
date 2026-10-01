"""close with initialized submodules, failed removal and retry. Real git in a temp dir; ORCHD_HOME never touched."""
import os
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from orchd import core, store
from orchd.runtime import Runtime, error_detail, redact

GIT = ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "protocol.file.allow=always"]


def git(*args, cwd):
    return subprocess.run([*GIT, *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class HookRuntime(Runtime):
    """Runs a callback once, right before `git worktree remove`, to inject a change after all checks passed."""
    before_remove = None
    after_state = None  # runs once, right after the first state check, i.e. before removal re-checks
    jobs = {}  # what `claude agents` lists; tests never reach the real claude CLI
    stop_result = None  # what `claude stop` returns or raises; None means it succeeds

    def agents(self):
        if isinstance(self.jobs, Exception):
            raise self.jobs
        return [dict(job, id=job_id) for job_id, job in self.jobs.items()]

    def sleep(self, seconds):
        pass

    def worktree_state(self, worktree, base):
        result = super().worktree_state(worktree, base)
        if self.after_state:
            hook, self.after_state = self.after_state, None
            hook()
        return result

    def run(self, cmd, *a, **kw):
        if cmd[:2] == [self.claude, "stop"]:
            if isinstance(self.stop_result, BaseException):
                raise self.stop_result
            return self.stop_result or subprocess.CompletedProcess(cmd, 0, "", "")
        if "remove" in cmd and "worktree" in cmd and self.before_remove:
            hook, self.before_remove = self.before_remove, None
            hook()
        return super().run(cmd, *a, **kw)


class CloseSubmoduleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        sub_remote, self.remote = root / "sub.git", root / "remote.git"
        for bare in (sub_remote, self.remote):
            git("init", "-q", "--bare", "-b", "main", str(bare), cwd=root)
        seed = root / "seed"
        seed.mkdir()
        git("init", "-q", "-b", "main", cwd=seed)
        git("commit", "-q", "--allow-empty", "-m", "s", cwd=seed)
        git("remote", "add", "origin", str(sub_remote), cwd=seed)
        git("push", "-q", "origin", "main", cwd=seed)
        self.projects = root / "projects"
        repo = self.projects / "demo"
        repo.mkdir(parents=True)
        git("init", "-q", "-b", "main", cwd=repo)
        git("commit", "-q", "--allow-empty", "-m", "i", cwd=repo)
        git("submodule", "add", "-q", str(sub_remote), "vendor", cwd=repo)
        git("commit", "-q", "-m", "add submodule", cwd=repo)
        git("remote", "add", "origin", str(self.remote), cwd=repo)
        git("push", "-q", "-u", "origin", "main", cwd=repo)
        git("remote", "set-head", "origin", "main", cwd=repo)
        os.environ["ORCHD_PROJECTS"] = str(self.projects)
        self.rt = HookRuntime()
        self.repo = self.rt.repo_path("demo")
        self.base, self.branch, self.wt = self.rt.create_worktree(self.repo, "demo", "abcd1234")
        git("submodule", "update", "-q", "--init", cwd=self.wt)
        self.sub = os.path.join(self.wt, "vendor")
        self.con = store.connect(root / "t.db")
        store.create_task(self.con, id="abcd1234", repo="demo", repo_path=str(self.repo), title="T",
                          instructions="i", done_when="d", orch_thread="th", codex_bin="c", model="claude-sonnet-5-5",
                          base=self.base, branch=self.branch, worktree=self.wt, status="done")

    def tearDown(self):
        os.environ.pop("ORCHD_PROJECTS", None)
        self.tmp.cleanup()

    def close_events(self):
        return self.con.execute("SELECT body FROM messages WHERE kind='close'").fetchall()

    def assert_kept(self, text):
        result = core.close(self.con, self.rt, "abcd1234")
        self.assertIn(text, result["worktree"])
        self.assertTrue(Path(self.wt).exists())
        return result

    def test_clean_submodule_worktree_is_closed_but_kept_because_git_cannot_remove_it_safely(self):
        result = core.close(self.con, self.rt, "abcd1234", outcome="merged")
        self.assertIn("kept at", result["worktree"])
        self.assertIn("submodule", result["worktree"])
        self.assertTrue(Path(self.wt).exists())
        self.assertEqual(store.get_task(self.con, "abcd1234")["status"], "closed")
        self.assertEqual(len(self.close_events()), 1)

    def test_untracked_file_in_submodule_is_kept(self):
        Path(self.sub, "u.txt").write_text("keep me")
        self.assert_kept("kept at")
        self.assertEqual(Path(self.sub, "u.txt").read_text(), "keep me")

    def test_dirty_tracked_file_in_submodule_is_kept(self):
        Path(self.sub, "t.txt").write_text("a")
        git("add", "t.txt", cwd=self.sub)
        self.assert_kept("kept at")

    def test_unpushed_commit_in_submodule_is_kept(self):
        git("commit", "-q", "--allow-empty", "-m", "local only", cwd=self.sub)
        self.assert_kept("kept at")
        self.assertIn("worktree kept", store.get_task(self.con, "abcd1234")["note"])

    def test_unpushed_local_branch_in_submodule_is_kept_even_when_detached_head_is_pushed(self):
        git("branch", "wip", cwd=self.sub)
        git("checkout", "-q", "--detach", "origin/main", cwd=self.sub)
        git("commit", "-q", "--allow-empty", "-m", "on wip", cwd=self.sub)
        git("checkout", "-q", "--detach", "origin/main", cwd=self.sub)
        git("branch", "-f", "wip", "HEAD@{1}", cwd=self.sub)
        self.assertEqual(self.rt.worktree_state(self.wt, self.base)[0], False)

    def test_stash_in_submodule_is_kept(self):
        Path(self.sub, "t.txt").write_text("a")
        git("add", "t.txt", cwd=self.sub)
        git("stash", "-q", cwd=self.sub)
        self.assertEqual(self.rt.worktree_state(self.wt, self.base)[1], "stash in submodule vendor")

    def test_dirty_parent_is_kept_without_force(self):
        Path(self.wt, "p.txt").write_text("x")
        self.assert_kept("uncommitted changes")

    def plain_task(self, task_id="efgh5678", **fields):
        """A worktree whose submodule is not initialized, so git itself is allowed to remove it."""
        base, branch, wt = self.rt.create_worktree(self.repo, "demo", task_id)
        fields = dict(dict(model="claude-sonnet-5-5"), **fields)
        store.create_task(self.con, id=task_id, repo="demo", repo_path=str(self.repo), title="T", instructions="i",
                          done_when="d", orch_thread="th", codex_bin="c", base=base, branch=branch, worktree=wt,
                          status="done", **fields)
        return wt

    def hide_untracked(self):
        git("config", "status.showUntrackedFiles", "no", cwd=self.repo)  # shared by every worktree

    def test_plain_worktree_is_still_removed(self):
        wt = self.plain_task()
        self.assertEqual(core.close(self.con, self.rt, "efgh5678")["worktree"], "removed")
        self.assertFalse(Path(wt).exists())

    def test_hidden_untracked_setting_does_not_hide_parent_untracked_file(self):
        self.hide_untracked()
        Path(self.wt, "u.txt").write_text("keep me")
        self.assert_kept("uncommitted changes")
        self.assertEqual(Path(self.wt, "u.txt").read_text(), "keep me")
        wt = self.plain_task()
        Path(wt, "u.txt").write_text("keep me too")
        result = core.close(self.con, self.rt, "efgh5678")
        self.assertIn("kept at", result["worktree"])
        self.assertEqual(Path(wt, "u.txt").read_text(), "keep me too")

    def test_untracked_file_created_after_the_check_survives_even_with_hidden_setting(self):
        self.hide_untracked()
        wt = self.plain_task()
        self.rt.before_remove = lambda: Path(wt, "late.txt").write_text("late")
        result = core.close(self.con, self.rt, "efgh5678")
        self.assertIn("kept at", result["worktree"])
        self.assertEqual(Path(wt, "late.txt").read_text(), "late")

    def test_submodule_stash_and_untracked_survive_and_git_is_never_asked_to_remove(self):
        asked = []
        real_run = self.rt.run
        self.rt.run = lambda cmd, *a, **kw: (asked.append(cmd), real_run(cmd, *a, **kw))[1]
        self.rt.after_state = lambda: (Path(self.sub, "late.txt").write_text("late"),
                                       git("add", "late.txt", cwd=self.sub), git("stash", "-q", cwd=self.sub),
                                       Path(self.sub, "late2.txt").write_text("late2"))
        result = core.close(self.con, self.rt, "abcd1234")
        self.assertIn("kept at", result["worktree"])
        self.assertEqual(Path(self.sub, "late2.txt").read_text(), "late2")
        self.assertIn("late.txt", git("stash", "show", "--name-only", cwd=self.sub))
        self.assertFalse([c for c in asked if "remove" in c and "worktree" in c])

    def test_commit_created_after_the_check_keeps_the_worktree(self):
        wt = self.plain_task()
        self.rt.after_state = lambda: git("commit", "-q", "--allow-empty", "-m", "late", cwd=wt)
        result = core.close(self.con, self.rt, "efgh5678")
        self.assertIn("commits not pushed", result["worktree"])
        self.assertTrue(Path(wt).exists())

    def test_git_error_in_state_check_carries_stderr(self):
        not_repo = Path(self.tmp.name, "plain")
        not_repo.mkdir()
        self.con.execute("UPDATE tasks SET worktree=? WHERE id='abcd1234'", (str(not_repo),))
        with self.assertRaisesRegex(RuntimeError, "not a git repository"):
            core.close(self.con, self.rt, "abcd1234")
        self.assertEqual(store.get_task(self.con, "abcd1234")["status"], "done")
        self.assertIn("not a git repository", store.get_task(self.con, "abcd1234")["note"])

    def test_db_failure_while_finishing_leaves_no_close_event_and_retry_emits_one(self):
        real = store.update_task
        calls = []

        def flaky(con, task_id, **fields):
            if fields.get("status") == "closed" and not calls:
                calls.append(1)
                raise sqlite3.OperationalError("database is locked")
            return real(con, task_id, **fields)
        store.update_task = flaky
        self.addCleanup(setattr, store, "update_task", real)
        with self.assertRaises(sqlite3.OperationalError):
            core.close(self.con, self.rt, "abcd1234", outcome="merged")
        self.assertEqual(self.close_events(), [])
        self.assertEqual(store.get_task(self.con, "abcd1234")["status"], "done")
        core.close(self.con, self.rt, "abcd1234")
        self.assertEqual(len(self.close_events()), 1)
        self.assertEqual(store.get_task(self.con, "abcd1234")["status"], "closed")
        self.assertEqual(store.get_task(self.con, "abcd1234")["outcome"], "merged")

    def test_tag_only_commit_in_submodule_is_kept(self):
        git("checkout", "-q", "--detach", cwd=self.sub)
        git("commit", "-q", "--allow-empty", "-m", "tagged", cwd=self.sub)
        git("tag", "saved", cwd=self.sub)
        git("checkout", "-q", "--detach", "origin/main", cwd=self.sub)
        self.assertEqual(self.rt.worktree_state(self.wt, self.base)[1], "commits not pushed in submodule vendor")

    def test_missing_worktree_can_be_closed(self):
        shutil.rmtree(self.wt)
        self.assertFalse(Path(self.wt).exists())
        self.assertEqual(core.close(self.con, self.rt, "abcd1234")["worktree"], "removed")
        self.assertEqual(store.get_task(self.con, "abcd1234")["status"], "closed")

    # -- the task's worker must be confirmed stopped before anything is removed or closed ------------------------
    def assert_close_pending(self, task_id, wt, text):
        with self.assertRaisesRegex(RuntimeError, text):
            core.close(self.con, self.rt, task_id, outcome="merged")
        task = store.get_task(self.con, task_id)
        self.assertTrue(Path(wt).exists())
        self.assertEqual(task["status"], "done")
        self.assertTrue(task["note"].startswith("close pending"), task["note"])
        self.assertEqual(self.close_events(), [])

    def test_failed_stop_of_a_still_running_claude_worker_keeps_worktree_and_task_open(self):
        wt = self.plain_task(job_id="job-live")
        Path(wt, "work.txt").write_text("in progress")
        git("add", "work.txt", cwd=wt)
        git("commit", "-q", "-m", "w", cwd=wt)
        git("push", "-q", "-u", "origin", "HEAD", cwd=wt)  # pushed, so only the live worker stands in the way
        self.rt.stop_result = subprocess.CompletedProcess(["claude", "stop"], 1, "", "synthetic stop failure")
        self.rt.jobs = {"job-live": dict(state="working", pid=os.getpid())}
        self.assert_close_pending("efgh5678", wt, "job-live.*synthetic stop failure")
        self.assertEqual(Path(wt, "work.txt").read_text(), "in progress")
        self.rt.jobs = {}  # the worker is gone now: retry closes it, once
        self.assertEqual(core.close(self.con, self.rt, "efgh5678")["worktree"], "removed")
        self.assertEqual(len(self.close_events()), 1)
        self.assertEqual(store.get_task(self.con, "efgh5678")["outcome"], "merged")

    def test_unknown_worker_state_keeps_worktree_and_task_open(self):
        wt = self.plain_task(job_id="job-x")
        self.rt.jobs = RuntimeError("claude agents: daemon unreachable")
        self.assert_close_pending("efgh5678", wt, "job-x")

    def test_listed_job_without_a_pid_is_not_taken_as_stopped(self):
        wt = self.plain_task(job_id="job-x")
        self.rt.jobs = {"job-x": dict(state="blocked", pid=None)}  # seen live for a never-stopped orch session
        self.assert_close_pending("efgh5678", wt, "pid unknown")

    def test_stop_timeout_keeps_worktree_and_reports_stderr(self):
        wt = self.plain_task(job_id="job-x")
        self.rt.stop_result = subprocess.TimeoutExpired(["claude", "stop", "job-x"], 30, stderr=b"stop hung here")
        self.rt.jobs = {"job-x": dict(state="working", pid=os.getpid())}
        self.assert_close_pending("efgh5678", wt, "stop hung here")

    def test_failed_stop_of_a_worker_that_is_already_gone_still_closes(self):
        wt = self.plain_task(job_id="job-gone")
        self.rt.stop_result = subprocess.CompletedProcess(["claude", "stop"], 1, "", "no such job")
        dead = subprocess.Popen(["true"])
        dead.wait()
        for listing in ({}, {"job-gone": dict(state="failed", pid=os.getpid())},
                        {"job-gone": dict(state="working", pid=dead.pid)}):
            self.rt.jobs = listing
            self.rt.stop_task_worker("claude", "job-gone")
        self.assertEqual(core.close(self.con, self.rt, "efgh5678")["worktree"], "removed")
        self.assertFalse(Path(wt).exists())

    # -- concurrent closes of one task from separate processes/connections -------------------------------------
    def test_concurrent_closes_write_one_close_event(self):
        wt = self.plain_task()
        db = self.con.execute("PRAGMA database_list").fetchone()["file"]
        start, results, errors = threading.Barrier(2), [], []
        real_state = Runtime.worktree_state

        class SlowRuntime(HookRuntime):
            def worktree_state(self, worktree, base):
                time.sleep(0.3)  # keep the first close busy while the second one arrives
                return real_state(self, worktree, base)

        def caller():
            con = store.connect(db)
            try:
                start.wait(5)
                results.append(core.close(con, SlowRuntime(), "efgh5678", outcome="merged"))
            except Exception as e:  # noqa: BLE001 -- surfaced by the assertion below
                errors.append(e)
            finally:
                con.close()
        threads = [threading.Thread(target=caller) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(r["worktree"] for r in results), ["already closed", "removed"])
        self.assertEqual(len(self.close_events()), 1)
        self.assertFalse(Path(wt).exists())

    def test_close_waits_a_bounded_time_for_another_close_then_leaves_task_retryable(self):
        wt = self.plain_task()
        with core._close_lock(self.con, "efgh5678"):
            started = time.monotonic()
            with self.assertRaisesRegex(RuntimeError, "already in progress"):
                core.close(self.con, self.rt, "efgh5678", lock_wait=0.3)
            self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(store.get_task(self.con, "efgh5678")["status"], "done")
        self.assertTrue(Path(wt).exists())
        self.assertEqual(core.close(self.con, self.rt, "efgh5678")["worktree"], "removed")
        self.assertEqual(len(self.close_events()), 1)

    def test_status_written_closed_by_someone_else_mid_close_emits_no_second_event(self):
        self.plain_task()
        other = store.connect(self.con.execute("PRAGMA database_list").fetchone()["file"])
        self.addCleanup(other.close)
        self.rt.after_state = lambda: store.update_task(other, "efgh5678", status="closed")  # a close not using the lock
        self.assertEqual(core.close(self.con, self.rt, "efgh5678")["worktree"], "already closed")
        self.assertEqual(self.close_events(), [])
        self.assertEqual(store.get_task(self.con, "efgh5678")["status"], "closed")

    def test_timeout_in_state_check_keeps_its_stderr(self):
        wt = self.plain_task()
        real_run = self.rt.run

        def slow(cmd, *a, **kw):
            if "status" in cmd:
                raise subprocess.TimeoutExpired(cmd, 60, output=b"partial out", stderr=b"fatal: precise timeout stderr")
            return real_run(cmd, *a, **kw)
        self.rt.run = slow
        with self.assertRaisesRegex(RuntimeError, "precise timeout stderr") as ctx:
            core.close(self.con, self.rt, "efgh5678")
        self.assertIn("timed out after 60s", str(ctx.exception))
        self.assertIn("precise timeout stderr", store.get_task(self.con, "efgh5678")["note"])
        self.assertTrue(Path(wt).exists())

class FailingRemoveRuntime:
    """Fake whose remove fails until told otherwise; counts stops so retries are visible."""

    def __init__(self, error):
        self.error, self.stopped, self.removed = error, [], []

    def stop_task_worker(self, kind, job, marks=()):
        self.stopped.append(job if kind == "claude" else (kind, job, marks))

    def worktree_state(self, worktree, base):
        return True, "pushed"

    def remove_worktree(self, repo_path, worktree, base=None):
        if self.error:
            raise self.error
        self.removed.append(worktree)

    claude_usage = Runtime.claude_usage


class CloseRetryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = store.connect(Path(self.tmp.name) / "t.db")
        store.create_task(self.con, id="t1", repo="demo", repo_path="/projects/demo", title="T", instructions="i",
                          done_when="d", orch_thread="th", codex_bin="c", model="claude-sonnet-5-5", base="b",
                          worktree="/wt/demo-t1", job_id="job1", status="done", note="earlier note")
        self.rt = FailingRemoveRuntime(RuntimeError("git worktree remove failed (exit 128): fatal: boom"))

    def tearDown(self):
        self.tmp.cleanup()

    def events(self, kind):
        return self.con.execute("SELECT body FROM messages WHERE kind=?", (kind,)).fetchall()

    def test_failed_remove_leaves_task_open_with_stderr_and_no_terminal_event(self):
        with self.assertRaisesRegex(RuntimeError, "fatal: boom"):
            core.close(self.con, self.rt, "t1", outcome="merged", rating=2)
        task = store.get_task(self.con, "t1")
        self.assertEqual(task["status"], "done")
        self.assertIn("fatal: boom", task["note"])
        self.assertEqual(self.events("close"), [])
        self.assertEqual([t["id"] for t in store.open_tasks(self.con)], ["t1"])

    def test_retry_emits_one_close_event_and_keeps_outcome(self):
        for _ in range(2):
            with self.assertRaises(RuntimeError):
                core.close(self.con, self.rt, "t1", outcome="merged", rating=2)
        self.rt.error = None
        self.assertEqual(core.close(self.con, self.rt, "t1")["worktree"], "removed")
        task = store.get_task(self.con, "t1")
        self.assertEqual((task["status"], task["outcome"], task["rating"]), ("closed", "merged", 2))
        self.assertIsNone(task["note"])
        self.assertEqual(len(self.events("close")), 1)
        self.assertEqual(core.close(self.con, self.rt, "t1")["worktree"], "already closed")
        self.assertEqual(len(self.events("close")), 1)

    def test_error_from_state_check_also_leaves_task_retryable(self):
        self.rt.worktree_state = lambda *a: (_ for _ in ()).throw(RuntimeError("fatal: not a git repository"))
        with self.assertRaisesRegex(RuntimeError, "not a git repository"):
            core.close(self.con, self.rt, "t1")
        self.assertEqual(store.get_task(self.con, "t1")["status"], "done")

    def test_credentials_in_urls_are_redacted(self):
        self.rt.error = RuntimeError("fatal: unable to access 'https://user:ghp_secret@github.com/x.git/'")
        with self.assertRaises(RuntimeError) as ctx:
            core.close(self.con, self.rt, "t1")
        self.assertNotIn("ghp_secret", str(ctx.exception))
        self.assertNotIn("ghp_secret", store.get_task(self.con, "t1")["note"])
        self.assertIn("***@github.com", redact("https://u:p@github.com"))

    def test_credentials_with_at_signs_encodings_and_several_urls_are_all_redacted(self):
        text = ("fatal: unable to access 'https://worker:p@ssword@github.com/private.git/'; "
                "also http://ghp_tok%40en:x%2Fy@example.com:8443/a and ssh://deploy:s3cr3t@host/r.git")
        out = redact(text)
        for secret in ("p@ssword", "ssword", "ghp_tok", "%40en", "x%2Fy", "s3cr3t", "worker", "deploy"):
            self.assertNotIn(secret, out)
        self.assertIn("https://***@github.com/private.git/", out)
        self.assertIn("http://***@example.com:8443/a", out)
        self.assertIn("ssh://***@host/r.git", out)
        self.assertEqual(redact("see https://github.com/a/b@v1?q=a@b"), "see https://github.com/a/b@v1?q=a@b")
        self.rt.error = RuntimeError(text)
        with self.assertRaises(RuntimeError) as ctx:
            core.close(self.con, self.rt, "t1")
        for where in (str(ctx.exception), store.get_task(self.con, "t1")["note"]):
            self.assertNotIn("ssword", where)
            self.assertNotIn("s3cr3t", where)

    def test_error_detail_keeps_stdout_stderr_and_timeout_and_redacts_them(self):
        cmd = ["git", "fetch", "https://u:tok@h/r"]
        self.assertEqual(error_detail(subprocess.CalledProcessError(128, cmd, output="", stderr="fatal: e\n")),
                         "fatal: e")
        self.assertEqual(error_detail(subprocess.CalledProcessError(1, cmd, output="only stdout https://a:b@h/x")),
                         "only stdout https://***@h/x")
        detail = error_detail(subprocess.TimeoutExpired(cmd, 60, output=b"out https://u:tok@h", stderr=b"err"))
        self.assertIn("timed out after 60s", detail)
        self.assertIn("err", detail)
        self.assertIn("out https://***@h", detail)
        self.assertNotIn("tok", detail)
        self.assertEqual(error_detail(threading.BrokenBarrierError()), "BrokenBarrierError")
        self.assertIn("timed out after 5s", error_detail(subprocess.TimeoutExpired(cmd, 5)))
        self.assertNotIn("tok", error_detail(subprocess.TimeoutExpired(cmd, 5)))

    def test_codex_worker_is_stopped_only_when_it_is_this_tasks_process_and_confirmed_gone(self):
        rt, killed, ps = Runtime(), [], {"out": "", "rc": 0}
        rt.sleep = lambda s: None
        rt.run = lambda cmd, **kw: subprocess.CompletedProcess(cmd, ps["rc"], ps["out"], "")
        alive = {"4242": True}
        rt.pid_alive = lambda pid: alive[pid]
        orig, os.killpg = os.killpg, lambda pid, sig: killed.append(pid)
        self.addCleanup(setattr, os, "killpg", orig)
        ps["out"] = "/usr/bin/python3 other.py\n"  # pid reused by something else: ours is gone, kill nothing
        rt.stop_task_worker("codex", "4242", ("/wt/demo-t1", "thread-1"))
        ps["out"] = "/Users/x/.local/bin/codex exec resume --json thread-9 hi\n"  # another task's codex
        rt.stop_task_worker("codex", "4242", ("/wt/demo-t1", "thread-1"))
        self.assertEqual(killed, [])
        ps["out"] = "/Users/x/.local/bin/codex exec resume --json thread-1 hi\n"
        with self.assertRaisesRegex(RuntimeError, "still running"):  # SIGTERM sent but it does not exit
            rt.stop_task_worker("codex", "4242", ("/wt/demo-t1", "thread-1"))
        self.assertEqual(killed, [4242])
        rt.pid_alive = lambda pid: not killed  # exits after SIGTERM
        killed.clear()
        rt.stop_task_worker("codex", "4242", ("/wt/demo-t1", "thread-1"))
        self.assertEqual(killed, [4242])
        rt.pid_alive = lambda pid: True
        ps["rc"], ps["out"] = 2, ""  # ps itself broke: unknown, so not stopped
        with self.assertRaisesRegex(RuntimeError, "not confirmed"):
            rt.stop_task_worker("codex", "4242", ("/wt/demo-t1", "thread-1"))

    def test_close_of_codex_task_stops_it_through_the_confirming_path(self):
        self.con.execute("UPDATE tasks SET model='gpt-6.1-sol', session_id='thread-1' WHERE id='t1'")
        self.rt.error = None
        core.close(self.con, self.rt, "t1")
        self.assertEqual(self.rt.stopped, [("codex", "job1", ("/wt/demo-t1", "thread-1"))])


if __name__ == "__main__":
    unittest.main()
