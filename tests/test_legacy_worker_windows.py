"""Legacy exec stop uses task spawn identities on Windows, session leaders on POSIX."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import psutil

from orchd import runtime


class LegacyWorkerPlatformTests(unittest.TestCase):
    def setUp(self):
        self.rt = runtime.Runtime.__new__(runtime.Runtime)
        self.worktree = str(Path.cwd().resolve())
        self.proc = Mock(pid=987601)
        self.proc.status.return_value = psutil.STATUS_RUNNING
        self.proc.create_time.return_value = 100
        self.record = dict(pid=self.proc.pid, birth=100, pgid=self.proc.pid, worktree=self.worktree)
        # Replace this module's os reference, not pathlib's platform selection.
        self.platform = Mock(wraps=os)
        self.platform.name = "nt"
        self.platform.getpgid = Mock(side_effect=AssertionError("Unix-only API on Windows"))
        patcher = patch.object(runtime, "os", self.platform)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_windows_spawn_identity_is_recorded_without_getpgid(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pids" / "987601.json"
            with patch.object(self.rt, "_identity_file", return_value=path), \
                 patch.object(runtime.psutil, "Process", return_value=self.proc):
                self.rt._record_identity(self.proc.pid, self.worktree)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), self.record)
        self.platform.getpgid.assert_not_called()

    def test_windows_unrecorded_pid_reaches_marked_cleanup_without_signals(self):
        # The reviewer's real-current-PID reproduction; no signal can escape this test.
        with patch.object(runtime, "task_processes", return_value={}), \
             patch.object(runtime, "cleanup_processes", return_value={"exited": []}) as cleanup, \
             patch.object(self.rt, "_read_identity", return_value=None), \
             patch.object(self.rt, "_signal_codex") as signal:
            result = self.rt.stop_task_worker("codex", str(os.getpid()), (self.worktree,), wait=0)
        self.assertEqual(result, {"exited": []})
        cleanup.assert_called_once_with(self.worktree, {})
        signal.assert_not_called()
        self.platform.getpgid.assert_not_called()

    def test_windows_recorded_task_pid_is_stopped_then_cleanup_runs(self):
        with patch.object(runtime.psutil, "Process", return_value=self.proc), \
             patch.object(self.rt, "_read_identity", return_value=self.record), \
             patch.object(self.rt, "pid_alive", side_effect=[True, False]), \
             patch.object(self.rt, "sleep"), \
             patch.object(runtime, "task_processes", return_value={}), \
             patch.object(runtime, "cleanup_processes", return_value=None) as cleanup:
            self.rt.stop_task_worker("codex", str(self.proc.pid), (self.worktree,), wait=0)
        self.proc.terminate.assert_called_once()
        cleanup.assert_called_once_with(self.worktree, {})
        self.platform.getpgid.assert_not_called()

    def test_windows_reused_foreign_or_unowned_record_never_authorizes_signals(self):
        for changes in ({"birth": 99}, {"worktree": "another-task"}, {"worktree": None}):
            with self.subTest(changes=changes), \
                 patch.object(runtime.psutil, "Process", return_value=self.proc), \
                 patch.object(self.rt, "_read_identity", return_value={**self.record, **changes}):
                self.assertEqual(self.rt._codex_targets(self.proc.pid, (self.worktree,)), [])
        self.proc.terminate.assert_not_called()

    def test_windows_unreadable_identity_blocks_but_cleanup_still_runs(self):
        self.proc.create_time.side_effect = psutil.AccessDenied(self.proc.pid)
        with patch.object(runtime.psutil, "Process", return_value=self.proc), \
             patch.object(self.rt, "pid_alive", return_value=True), \
             patch.object(runtime, "task_processes", return_value={}), \
             patch.object(runtime, "cleanup_processes", return_value=None) as cleanup:
            with self.assertRaisesRegex(RuntimeError, "cannot inspect it"):
                self.rt.stop_task_worker("codex", str(self.proc.pid), (self.worktree,), wait=0)
        cleanup.assert_called_once()
        self.proc.terminate.assert_not_called()

    def test_posix_still_requires_recorded_process_group_leader(self):
        self.platform.name = "posix"
        self.platform.getpgid.side_effect = None
        self.platform.getpgid.return_value = self.proc.pid
        with patch.object(runtime.psutil, "Process", return_value=self.proc), \
             patch.object(self.rt, "_read_identity", return_value=self.record):
            self.assertEqual(self.rt._codex_targets(self.proc.pid, (self.worktree,)), [self.proc])
            self.platform.getpgid.return_value = self.proc.pid - 1
            with self.assertRaisesRegex(RuntimeError, "process-group leader"):
                self.rt._codex_targets(self.proc.pid, (self.worktree,))
        self.assertEqual(self.platform.getpgid.call_count, 2)

    def test_posix_spawn_identity_still_records_real_group(self):
        self.platform.name = "posix"
        self.platform.getpgid.side_effect = None
        self.platform.getpgid.return_value = 12345
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "987601.json"
            with patch.object(self.rt, "_identity_file", return_value=path), \
                 patch.object(runtime.psutil, "Process", return_value=self.proc):
                self.rt._record_identity(self.proc.pid, self.worktree)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["pgid"], 12345)
        self.platform.getpgid.assert_called_once_with(self.proc.pid)
