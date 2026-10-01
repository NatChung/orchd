"""close with initialized submodules, failed removal and retry. Real git in a temp dir; ORCHD_HOME never touched."""
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

from orchd import core, store
from orchd.runtime import Runtime, redact

GIT = ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "protocol.file.allow=always"]


def git(*args, cwd):
    return subprocess.run([*GIT, *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class HookRuntime(Runtime):
    """Runs a callback once, right before `git worktree remove`, to inject a change after all checks passed."""
    before_remove = None
    after_state = None  # runs once, right after the first state check, i.e. before removal re-checks

    def worktree_state(self, worktree, base):
        result = super().worktree_state(worktree, base)
        if self.after_state:
            hook, self.after_state = self.after_state, None
            hook()
        return result

    def run(self, cmd, *a, **kw):
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

    def plain_task(self, task_id="efgh5678"):
        """A worktree whose submodule is not initialized, so git itself is allowed to remove it."""
        base, branch, wt = self.rt.create_worktree(self.repo, "demo", task_id)
        store.create_task(self.con, id=task_id, repo="demo", repo_path=str(self.repo), title="T", instructions="i",
                          done_when="d", orch_thread="th", codex_bin="c", model="claude-sonnet-5-5",
                          base=base, branch=branch, worktree=wt, status="done")
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


class FailingRemoveRuntime:
    """Fake whose remove fails until told otherwise; counts stops so retries are visible."""

    def __init__(self, error):
        self.error, self.stopped, self.removed = error, [], []

    def stop_worker(self, job):
        self.stopped.append(job)

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


if __name__ == "__main__":
    unittest.main()
