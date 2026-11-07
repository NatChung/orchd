"""Windows close: process ownership without uids, and app-server leftovers outside the worker job."""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import psutil

from orchd import app_worker, core, processes, store


class FakeProc:
    def __init__(self, pid, env=None, user="me", born=0.0, denied=False):
        self.pid, self._env, self._user, self._born, self._denied = pid, env or {}, user, born, denied

    def username(self):
        if self._user is None:
            raise psutil.AccessDenied(self.pid)
        return self._user

    def environ(self):
        if self._denied:
            raise psutil.AccessDenied(self.pid)
        return self._env

    def create_time(self):
        return self._born

    def children(self):
        return []

    def is_running(self):
        return True

    def status(self):
        return psutil.STATUS_RUNNING


@unittest.skipUnless(os.name == "nt", "Windows ownership rules")
class WindowsTaskProcessesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.worktree = str(Path(self.tmp.name).resolve())
        self.born = os.stat(self.worktree).st_ctime
        patcher = patch.object(processes, "_WINDOWS_USER", "me")
        patcher.start()
        self.addCleanup(patcher.stop)

    def scan(self, procs, worktree=None):
        with patch.object(processes.psutil, "process_iter", return_value=procs), \
             patch.object(processes.time, "sleep"):
            return processes.task_processes(worktree or self.worktree)

    def test_marked_process_of_this_user_is_found_without_uids(self):
        mine = FakeProc(10, {"ORCHD_WORKTREE": self.worktree}, born=self.born + 1)
        other = FakeProc(11, {"ORCHD_WORKTREE": self.worktree}, user="someone-else", born=self.born + 1)
        self.assertEqual(list(self.scan([mine, other])), [10])

    def test_unknown_owner_with_matching_or_unreadable_mark_blocks_without_signalling(self):
        for denied in (False, True):
            with self.subTest(denied=denied):
                proc = FakeProc(12, {"ORCHD_WORKTREE": self.worktree}, user=None,
                                born=self.born + 1, denied=denied)
                proc.terminate = MagicMock()
                proc.kill = MagicMock()
                with self.assertRaisesRegex(RuntimeError, "cannot inspect process 12"):
                    self.scan([proc])
                proc.terminate.assert_not_called()
                proc.kill.assert_not_called()

    def test_unknown_owner_predating_worktree_is_skipped(self):
        proc = FakeProc(13, user=None, born=self.born - 1, denied=True)
        self.assertEqual(self.scan([proc]), {})

    def test_unknown_owner_with_readable_unrelated_mark_is_skipped(self):
        proc = FakeProc(14, {"ORCHD_WORKTREE": "another"}, user=None, born=self.born + 1)
        self.assertEqual(self.scan([proc]), {})

    def test_unreadable_helper_older_than_the_worktree_is_skipped(self):
        ctfmon = FakeProc(20, born=self.born - 60, denied=True)
        self.assertEqual(self.scan([ctfmon]), {})

    def test_unreadable_process_newer_than_the_worktree_keeps_close_fail_closed(self):
        late = FakeProc(21, born=self.born + 1, denied=True)
        with self.assertRaisesRegex(RuntimeError, "cannot inspect process 21"):
            self.scan([late])

    def test_gone_worktree_skips_unreadable_processes(self):
        late = FakeProc(22, born=time.time(), denied=True)
        self.assertEqual(self.scan([late], worktree=str(Path(self.worktree) / "gone")), {})


    def test_unmarked_descendant_is_not_selected(self):
        root = FakeProc(30, {"ORCHD_WORKTREE": self.worktree}, born=self.born + 1)
        child = FakeProc(31, born=self.born + 1)
        root.children = lambda: [child]
        self.assertEqual(list(self.scan([root])), [30])

    def test_marked_descendant_of_another_account_is_not_selected(self):
        root = FakeProc(30, {"ORCHD_WORKTREE": self.worktree}, born=self.born + 1)
        child = FakeProc(31, {"ORCHD_WORKTREE": self.worktree},
                         user="someone-else", born=self.born + 1)
        root.children = lambda: [child]
        for proc in (root, child):
            proc.is_running = MagicMock(return_value=True)
            proc.terminate = MagicMock(side_effect=lambda p=proc: setattr(p.is_running, "return_value", False))
            proc.kill = MagicMock()
        with patch.object(processes.psutil, "process_iter", return_value=[root, child]):
            receipt = processes.cleanup_processes(self.worktree, {})
        self.assertEqual(receipt["terminated"], [30])
        child.terminate.assert_not_called()
        child.kill.assert_not_called()

    def test_marked_descendant_with_unknown_owner_blocks_cleanup_without_signalling(self):
        root = FakeProc(30, {"ORCHD_WORKTREE": self.worktree}, born=self.born + 1)
        child = FakeProc(31, {"ORCHD_WORKTREE": self.worktree},
                         user=None, born=self.born + 1)
        root.children = lambda: [child]
        child.username = MagicMock(wraps=child.username)
        for proc in (root, child):
            proc.terminate = MagicMock()
            proc.kill = MagicMock()
        with patch.object(processes.psutil, "process_iter", return_value=[root]):
            with self.assertRaisesRegex(RuntimeError, "cannot inspect process 31"):
                processes.cleanup_processes(self.worktree, {})
        for proc in (root, child):
            proc.terminate.assert_not_called()
            proc.kill.assert_not_called()
        child.username.assert_called_once()

    def test_same_user_marked_descendant_missing_from_snapshot_is_cleaned(self):
        root = FakeProc(30, {"ORCHD_WORKTREE": self.worktree}, born=self.born + 1)
        child = FakeProc(31, {"ORCHD_WORKTREE": self.worktree}, born=self.born + 1)
        root.children = lambda: [child]
        for proc in (root, child):
            proc.is_running = MagicMock(return_value=True)
            proc.terminate = MagicMock(side_effect=lambda p=proc: setattr(p.is_running, "return_value", False))
            proc.kill = MagicMock()
        with patch.object(processes.psutil, "process_iter", return_value=[root]):
            receipt = processes.cleanup_processes(self.worktree, {})
        self.assertEqual(receipt, {"terminated": [30, 31], "killed": [], "exited": [30, 31]})
        child.terminate.assert_called_once()

    def test_unknown_descendant_owner_uses_independent_exclusions(self):
        root = FakeProc(30, {"ORCHD_WORKTREE": self.worktree}, born=self.born + 1)
        for child in (FakeProc(31, user=None, born=self.born - 1, denied=True),
                      FakeProc(32, {"ORCHD_WORKTREE": "another"}, user=None, born=self.born + 1)):
            with self.subTest(pid=child.pid):
                root.children = lambda: [child]
                self.assertEqual(list(self.scan([root])), [30])

    def test_worktree_stat_permission_error_is_not_missing(self):
        with patch.object(processes.os, "stat", side_effect=PermissionError("denied")):
            with self.assertRaises(PermissionError):
                processes._predates_worktree(self.worktree)


class WindowsAppWorkerStopTests(unittest.TestCase):
    """The job does not contain wt-launched viewers or WSL shells; close must still stop them."""

    def setUp(self):
        self.con = MagicMock()
        self.con.execute.return_value.fetchall.return_value = [{"identity": "viewer-1"}]
        self.task = {"id": "t1", "generation": "g" * 32, "worktree": "C:/wt/t1",
                     "supervisor_identity": "sup", "server_identity": "srv"}
        self.alive = {"viewer-1"}

    def stop(self, terminated=True, swept=None):
        stopped = []

        def stop_identity(raw):
            stopped.append(raw)
            self.alive.discard(raw)

        job = MagicMock()
        job.terminate.return_value = terminated
        with patch.object(app_worker.os, "name", "nt"), \
             patch("orchd.windows_job", job, create=True), \
             patch.object(app_worker, "process", side_effect=lambda raw: object() if raw in self.alive else None), \
             patch.object(app_worker, "stop_identity", side_effect=stop_identity), \
             patch.object(app_worker, "cleanup_processes", return_value=swept) as cleanup:
            result = app_worker.stop(self.con, self.task)
        return result, stopped, cleanup

    def test_dead_job_still_stops_viewers_and_sweeps_escaped_processes(self):
        receipt = {"terminated": [3096], "killed": [], "exited": [3096]}
        result, stopped, cleanup = self.stop(swept=receipt)
        self.assertEqual(stopped, ["viewer-1"])
        cleanup.assert_called_once_with("C:/wt/t1", {})
        self.assertEqual(result, {"escaped": receipt})

    def test_live_job_is_terminated_then_viewers_stopped(self):
        self.alive |= {"sup"}
        job_alive = self.alive

        def terminate(generation, supervisor):
            job_alive.discard("sup")
            return True

        job = MagicMock()
        job.terminate.side_effect = terminate
        with patch.object(app_worker.os, "name", "nt"), \
             patch("orchd.windows_job", job, create=True), \
             patch.object(app_worker, "process", side_effect=lambda raw: object() if raw in self.alive else None), \
             patch.object(app_worker, "stop_identity", side_effect=lambda raw: self.alive.discard(raw)), \
             patch.object(app_worker, "cleanup_processes", return_value=None):
            result = app_worker.stop(self.con, self.task)
        self.assertEqual(result, {"terminated_job": "g" * 32})
        self.assertEqual(self.alive, set())

    def test_viewer_that_survives_keeps_the_task_open(self):
        with patch.object(app_worker.os, "name", "nt"), \
             patch("orchd.windows_job", MagicMock(), create=True), \
             patch.object(app_worker, "process", side_effect=lambda raw: object() if raw == "viewer-1" else None), \
             patch.object(app_worker, "stop_identity"), \
             patch.object(app_worker, "cleanup_processes") as cleanup:
            with self.assertRaisesRegex(RuntimeError, "viewer not confirmed stopped"):
                app_worker.stop(self.con, self.task)
        cleanup.assert_not_called()


@unittest.skipUnless(os.name == "nt", "Windows viewer lifecycle")
class LegacyViewerLifecycleTests(unittest.TestCase):
    def test_view_then_close_stops_registered_viewer_before_removal(self):
        with tempfile.TemporaryDirectory() as directory:
            con = store.connect(Path(directory) / "db.sqlite")
            store.create_task(con, id="abc", repo="demo", repo_path=directory, title="T",
                              instructions="i", done_when="d", orch_thread="th", codex_bin="c",
                              model="claude-sonnet-5-5", base="main", branch="b",
                              worktree=directory, status="done")
            store.update_task(con, "abc", job_id="job")
            rt = MagicMock()
            rt.stop_task_worker.return_value = None
            rt.open_viewer.return_value = 123
            rt.worktree_state.return_value = (True, None)
            rt.remove_worktree.return_value = None
            events = []
            rt.remove_worktree.side_effect = lambda *a: events.append("remove")
            with patch.object(app_worker, "identity", return_value="viewer-id"), \
                 patch.object(app_worker, "stop_identity", side_effect=lambda raw: events.append(raw)), \
                 patch.object(core, "worker_kind", return_value="claude"):
                core.view(con, rt, "abc")
                core._close_locked(con, rt, "abc", None, None)
            self.assertEqual(events, ["viewer-id", "remove"])
            con.close()

    def test_stop_identity_does_not_signal_unmarked_child(self):
        root = MagicMock(pid=100)
        child = MagicMock(pid=101)
        root.children.return_value = [child]
        with patch.object(app_worker, "process", return_value=root), \
             patch.object(app_worker.psutil, "wait_procs", return_value=([], [])):
            app_worker.stop_identity(json.dumps({"pid": 100, "birth": 1, "pgid": 100}))
        root.terminate.assert_called_once()
        child.terminate.assert_not_called()


class SupervisorEncodingTests(unittest.TestCase):
    def test_startup_error_decodes_utf8_supervisor_output(self):
        with tempfile.TemporaryDirectory() as directory:
            con = MagicMock()
            con.execute.return_value.fetchone.return_value = (None, None, "db")
            child = MagicMock(pid=123, returncode=1)
            child.poll.return_value = 1
            def launch(*args, **kwargs):
                self.assertEqual(kwargs["env"]["PYTHONIOENCODING"], "utf-8")
                os.write(kwargs["stdout"].fileno(), "\u4e2d\u6587\u932f\u8aa4".encode("utf-8"))
                return child
            with patch.object(app_worker.tempfile, "mkdtemp", return_value=directory), \
                 patch.object(app_worker, "make_private"), patch.object(app_worker.store, "update_task"), \
                 patch.object(app_worker.store, "get_task", return_value={"session_id": None}), \
                 patch.object(app_worker, "identity", return_value="identity"), \
                 patch.object(app_worker, "stop"), patch.object(app_worker.subprocess, "Popen", side_effect=launch):
                with self.assertRaisesRegex(RuntimeError, "\u4e2d\u6587\u932f\u8aa4"):
                    app_worker.start(con, None, {"id": "t", "worktree": directory}, "p", "model")


if __name__ == "__main__":
    unittest.main()
