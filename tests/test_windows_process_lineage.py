"""Windows close scan: helpers that a system service starts after an old worktree must not block close forever.

Runs on any platform: os.name and the account lookup are patched, processes are mocks, nothing is signalled.
"""
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import psutil

from orchd import processes

ME = "HOST\\me"


def proc(pid, *, owner=ME, environ=None, parent=None, born=None):
    """owner=None: the owner cannot be read. environ=None: the environment cannot be read.
    parent: a mock, None (no live parent) or an exception class raised by parent()."""
    p = mock.Mock(spec=psutil.Process, pid=pid)
    if owner is None:
        p.username.side_effect = psutil.AccessDenied(pid)
    else:
        p.username.return_value = owner
    if environ is None:
        p.environ.side_effect = psutil.AccessDenied(pid)
    else:
        p.environ.return_value = environ
    if isinstance(parent, type) and issubclass(parent, Exception):
        p.parent.side_effect = parent(pid)
    else:
        p.parent.return_value = parent
    p.create_time.return_value = time.time() + 3600 if born is None else born
    p.is_running.return_value = True
    p.status.return_value = psutil.STATUS_RUNNING
    p.children.return_value = []
    return p


class WindowsLineageTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.worktree = Path(tmp.name) / "wt"
        self.worktree.mkdir()
        self.mark = str(self.worktree.resolve())
        for patcher in (mock.patch.object(processes.os, "name", "nt"),
                        mock.patch.object(processes, "_WINDOWS_USER", ME),
                        # With os.name patched, Path() would pick WindowsPath on POSIX and fail to instantiate.
                        mock.patch.object(processes, "Path", type(self.worktree)),
                        mock.patch.object(processes.time, "sleep")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def scan(self, *procs):
        with mock.patch.object(processes.psutil, "process_iter", return_value=list(procs)):
            return processes.task_processes(self.worktree)

    def service(self):
        return proc(4, owner="NT AUTHORITY\\SYSTEM")

    def test_own_helper_started_by_a_service_after_the_worktree_does_not_block(self):
        # ctfmon.exe / ChtIME.exe / taskhostw.exe after a new sign-in
        self.assertEqual(self.scan(proc(1744, parent=self.service())), {})

    def test_own_helper_whose_parent_owner_is_unreadable_does_not_block(self):
        self.assertEqual(self.scan(proc(1744, parent=proc(3812, owner=None))), {})

    def test_unknown_owner_started_by_a_service_or_without_parent_does_not_block(self):
        self.assertEqual(self.scan(proc(26456, owner=None, parent=None),
                                   proc(5128, owner=None, parent=proc(900, owner=None))), {})

    def test_own_unreadable_process_started_by_our_process_still_blocks(self):
        with self.assertRaisesRegex(RuntimeError, "cannot inspect process 77"):
            self.scan(proc(77, parent=proc(76, environ={})))

    def test_own_unreadable_process_without_live_parent_still_blocks(self):
        for parent in (None, psutil.NoSuchProcess):
            with self.subTest(parent=parent), self.assertRaisesRegex(RuntimeError, "cannot inspect process 78"):
                self.scan(proc(78, parent=parent))

    def test_unknown_owner_started_by_our_process_still_blocks(self):
        # e.g. a UAC-elevated child of a worker: AIS names the worker as parent
        with self.assertRaisesRegex(RuntimeError, "cannot inspect process 79"):
            self.scan(proc(79, owner=None, parent=proc(76, environ={})))

    def test_readable_mark_wins_over_lineage(self):
        marked = proc(80, environ={"ORCHD_WORKTREE": self.mark}, parent=self.service())
        self.assertEqual(self.scan(marked), {80: marked})
        with self.assertRaisesRegex(RuntimeError, "cannot inspect process 81"):
            # an unknown owner carrying the mark is never signalled and keeps the worktree
            self.scan(proc(81, owner=None, environ={"ORCHD_WORKTREE": self.mark}, parent=None))

    def test_unreadable_child_of_a_marked_process_still_blocks(self):
        worker = proc(90, environ={"ORCHD_WORKTREE": self.mark}, parent=self.service())
        worker.children.return_value = [proc(91, parent=worker)]
        with self.assertRaisesRegex(RuntimeError, "cannot inspect process 91"):
            self.scan(worker)

    def test_process_older_than_the_worktree_is_still_skipped(self):
        self.assertEqual(self.scan(proc(6248, parent=None, born=0)), {})


if __name__ == "__main__":
    unittest.main()
