"""Issue #139: close handles a non-git or None worktree and a None worker; it never deletes a directory that is
not a recognised orchd worktree. Real git, temp dirs and a temp DB only."""
import os
import tempfile
import unittest
from pathlib import Path

from orchd import core, store
from tests.test_close_hardening import WorktreeCase
from tests.test_close_recovery import git


class NonGitWorktreeTest(WorktreeCase):
    def point_at(self, path):
        store.update_task(self.con, "abcd1234", worktree=str(path))

    def test_plain_directory_closes_and_is_left_untouched(self):
        plain = Path(self.tmp.name, "plain")
        plain.mkdir()
        (plain / "keep.txt").write_text("precious")
        self.point_at(plain)
        result = core.close(self.con, self.rt, "abcd1234")
        task = store.get_task(self.con, "abcd1234")
        self.assertTrue(result["closed"])
        self.assertEqual(task["status"], "closed")
        self.assertIn("not a git worktree", task["note"])
        self.assertEqual((plain / "keep.txt").read_text(), "precious")

    def test_directory_inside_another_repo_is_not_taken_for_a_worktree(self):
        inner = Path(self.repo, "sub")
        inner.mkdir()
        (inner / "f.txt").write_text("x")
        self.point_at(inner)
        core.close(self.con, self.rt, "abcd1234")
        self.assertEqual(store.get_task(self.con, "abcd1234")["status"], "closed")
        self.assertTrue((inner / "f.txt").exists())
        self.assertTrue(Path(self.repo, ".git").exists())

    def test_remove_worktree_never_runs_git_on_a_plain_directory(self):
        plain = Path(self.tmp.name, "plain2")
        plain.mkdir()
        reason = self.rt.remove_worktree(str(self.repo), str(plain), self.base)
        self.assertIn("not a git worktree", reason)
        self.assertTrue(plain.exists())

    def test_a_real_worktree_is_still_removed(self):
        result = core.close(self.con, self.rt, "abcd1234")
        self.assertEqual(result["worktree"], "removed")
        self.assertFalse(Path(self.wt).exists())


class NoWorktreeTest(WorktreeCase):
    def test_app_server_task_without_worktree_closes(self):
        store.update_task(self.con, "abcd1234", worktree=None, backend="app-server", job_id=None)
        result = core.close(self.con, self.rt, "abcd1234")
        self.assertTrue(result["closed"])
        self.assertEqual(store.get_task(self.con, "abcd1234")["status"], "closed")

    def test_exec_task_without_worktree_or_job_closes(self):
        store.update_task(self.con, "abcd1234", worktree=None, job_id=None)
        self.assertTrue(core.close(self.con, self.rt, "abcd1234")["closed"])


if __name__ == "__main__":
    unittest.main()
