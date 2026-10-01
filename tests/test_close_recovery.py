"""close with initialized submodules, failed removal and retry. Real git in a temp dir; ORCHD_HOME never touched."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from orchd import core, store
from orchd.runtime import Runtime, redact

GIT = ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "protocol.file.allow=always"]


def git(*args, cwd):
    return subprocess.run([*GIT, *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


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
        self.rt = Runtime()
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

    def test_clean_submodule_worktree_is_closed_and_removed(self):
        self.assertEqual(core.close(self.con, self.rt, "abcd1234", outcome="merged")["worktree"], "removed")
        self.assertFalse(Path(self.wt).exists())
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

    def test_remove_worktree_refuses_to_force_when_data_appeared_after_the_check(self):
        Path(self.sub, "late.txt").write_text("late")
        with self.assertRaisesRegex(RuntimeError, "uncommitted changes"):
            self.rt.remove_worktree(self.repo, self.wt)
        self.assertEqual(Path(self.sub, "late.txt").read_text(), "late")

    def test_missing_worktree_can_be_closed(self):
        self.rt.remove_worktree(self.repo, self.wt)
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

    def remove_worktree(self, repo_path, worktree):
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
