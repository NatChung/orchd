"""Issue #96: close must not stick at "close pending" for a dead pid, a damaged submodule or a refused git removal,
and must never delete unsaved work. Real git and real processes in a temp dir; fake data only."""
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

import psutil

from orchd import core, processes, store
from tests.test_close_recovery import CloseSubmoduleTest, git


class DeadPidTest(unittest.TestCase):
    def vanishing(self):
        proc = mock.Mock(spec=psutil.Process, pid=999999)
        proc.is_running.side_effect = [True, False, False, False, False, False]
        proc.status.return_value = psutil.STATUS_SLEEPING
        proc.children.side_effect = psutil.NoSuchProcess(999999)
        return proc

    def test_pid_that_vanishes_while_scanned_is_not_an_error(self):
        self.assertEqual(processes.task_processes("/nonexistent/wt", [self.vanishing()]), {})

    def test_cleanup_treats_a_vanished_pid_as_stopped(self):
        receipt = processes.cleanup_processes("/nonexistent/wt", {999999: self.vanishing()})
        self.assertEqual(receipt["killed"], [])


class StuckCloseTest(CloseSubmoduleTest):
    def test_close_succeeds_when_task_pid_vanishes_mid_scan_and_still_stops_the_live_one(self):
        env = dict(os.environ, ORCHD_WORKTREE=str(Path(self.wt).resolve()))
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], env=env)
        self.addCleanup(child.kill)
        with mock.patch.object(psutil.Process, "children", side_effect=psutil.NoSuchProcess(child.pid)):
            result = core.close(self.con, self.rt, "abcd1234")
        child.wait(timeout=10)
        self.assertEqual(store.get_task(self.con, "abcd1234")["status"], "closed")
        self.assertIn("kept at", result["worktree"])

    def test_unreadable_submodule_is_kept_and_task_closes_with_the_reason(self):
        git_dir = Path(git("rev-parse", "--absolute-git-dir", cwd=self.sub))
        (git_dir / "HEAD").write_text("garbage\n")
        result = core.close(self.con, self.rt, "abcd1234")
        task = store.get_task(self.con, "abcd1234")
        self.assertEqual(task["status"], "closed")
        self.assertIn("cannot verify submodule", result["worktree"])
        self.assertIn("cannot verify submodule", task["note"])
        self.assertTrue(Path(self.sub).exists())

    def test_unpushed_submodule_commit_survives_close_and_is_reported(self):
        git("commit", "-q", "--allow-empty", "-m", "unsaved", cwd=self.sub)
        head = git("rev-parse", "HEAD", cwd=self.sub)
        result = core.close(self.con, self.rt, "abcd1234")
        self.assertIn("kept at", result["worktree"])
        self.assertEqual(git("rev-parse", "HEAD", cwd=self.sub), head)
        self.assertEqual(store.get_task(self.con, "abcd1234")["status"], "closed")

    def test_git_refusing_removal_keeps_the_worktree_and_closes_with_the_reason(self):
        self.rt.submodule_loss = lambda worktree: None  # reach git's own removal
        git("worktree", "lock", self.wt, cwd=self.repo)
        result = core.close(self.con, self.rt, "abcd1234")
        task = store.get_task(self.con, "abcd1234")
        self.assertEqual(task["status"], "closed")
        self.assertIn("git worktree remove failed", result["worktree"])
        self.assertIn("git worktree remove failed", task["note"])
        self.assertTrue(Path(self.wt).exists())

    def test_stuck_closes_lists_pending_tasks_read_only(self):
        self.rt.stop_result = RuntimeError("boom")
        self.rt.jobs = {"job1": dict(pid=None, status="running")}
        self.con.execute("UPDATE tasks SET job_id='job1' WHERE id='abcd1234'")
        with self.assertRaises(RuntimeError):
            core.close(self.con, self.rt, "abcd1234")
        before = dict(store.get_task(self.con, "abcd1234"))
        rows = core.stuck_closes(self.con)
        self.assertEqual([r["task_id"] for r in rows], ["abcd1234"])
        self.assertIn("close pending", rows[0]["last_error"])
        self.assertIn("orchd close abcd1234", rows[0]["next_step"])
        self.assertEqual(dict(store.get_task(self.con, "abcd1234")), before)
        self.rt.jobs, self.rt.stop_result = {}, None
        core.close(self.con, self.rt, "abcd1234")
        self.assertEqual(core.stuck_closes(self.con), [])


for _name in dir(CloseSubmoduleTest):  # reuse the fixture only; do not rerun the inherited tests
    if _name.startswith("test_") and _name not in StuckCloseTest.__dict__:
        setattr(StuckCloseTest, _name, None)


if __name__ == "__main__":
    unittest.main()
