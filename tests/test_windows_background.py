import os
import subprocess
import tempfile
import time
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

from orchd import platform_process, quota
from orchd.runtime import Runtime


class BackgroundOptionsTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows creation flags")
    def test_background_and_detached_windows_flags(self):
        with patch.object(platform_process.os, 'name', 'nt'):
            self.assertEqual(platform_process.background_options(), {
                'creationflags': subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP})
            self.assertEqual(platform_process.detached_options(), {
                'creationflags': subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP})

    def test_posix_options_unchanged(self):
        with patch.object(platform_process.os, 'name', 'posix'):
            self.assertEqual(platform_process.background_options(), {'start_new_session': True})
            self.assertEqual(platform_process.detached_options(), {'start_new_session': True})

    @unittest.skipUnless(os.name == 'nt', 'Windows launches')
    def test_retry_and_exec_are_detached_and_use_windowless_python(self):
        with tempfile.TemporaryDirectory() as tmp, patch('orchd.runtime.worker_env', return_value={}), \
             patch('orchd.runtime.subprocess.Popen') as popen, \
             patch.object(Runtime, '_record_identity'), patch('orchd.store.home', return_value=Path(tmp)):
            popen.return_value.pid = 123
            rt = Runtime()
            rt.spawn(['codex', 'exec'], tmp, str(Path(tmp) / 'worker.log'))
            self.assertEqual(popen.call_args.kwargs['creationflags'],
                             subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS)
            rt.start_auto_retry('db', 'task', 1)
            self.assertTrue(popen.call_args.args[0][0].lower().endswith('pythonw.exe'))
            self.assertEqual(popen.call_args.kwargs['creationflags'],
                             subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS)

    @unittest.skipUnless(os.name == 'nt', 'Windows launches')
    def test_quota_probe_has_no_window_and_keeps_redirected_output(self):
        child = MagicMock(returncode=0)
        child.communicate.return_value = ('output', 'error')
        with patch('orchd.quota.subprocess.Popen', return_value=child) as popen:
            result = quota._run(['probe'], timeout=1, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(result.stdout, 'output')
        self.assertEqual(popen.call_args.kwargs['creationflags'],
                         subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW)
        self.assertEqual(popen.call_args.kwargs['stdout'], subprocess.PIPE)

    @unittest.skipUnless(os.name == 'nt', 'Windows viewer')
    def test_windows_viewer_requests_new_terminal_window(self):
        task = {'worktree': os.getcwd(), 'id': 'task', 'generation': 'gen'}
        with patch('orchd.runtime.worker_env', return_value={}), \
             patch('orchd.runtime.shutil.which', return_value='wt.exe'), \
             patch('orchd.runtime.subprocess.Popen') as popen:
            Runtime().open_app_viewer('db', task)
        self.assertEqual(popen.call_args.args[0][:3], ['wt.exe', '-w', 'new'])
        self.assertEqual(popen.call_args.args[0][-4:], ['viewer', 'db', 'task', 'gen'])

    @unittest.skipUnless(os.name == 'nt', 'Windows viewer')
    def test_windows_viewer_opens_new_console_when_terminal_unavailable(self):
        task = {'worktree': os.getcwd(), 'id': 'task', 'generation': 'gen'}
        with patch('orchd.runtime.worker_env', return_value={}), patch('orchd.runtime.shutil.which', return_value=None), \
             patch('orchd.runtime.subprocess.Popen') as popen:
            Runtime().open_app_viewer('db', task)
        self.assertEqual(popen.call_args.kwargs['creationflags'], subprocess.CREATE_NEW_CONSOLE)
        self.assertEqual(popen.call_args.args[0][-4:], ['viewer', 'db', 'task', 'gen'])


@unittest.skipUnless(os.name == 'nt', 'Windows viewer')
class WindowsForegroundViewerTests(unittest.TestCase):
    """Orch / Claude / Codex viewers must not shell out to macOS `open -na Ghostty.app` on Windows."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.exe = str(Path(self.tmp.name) / 'claude.exe')
        Path(self.exe).write_text('')
        self.rt = Runtime.__new__(Runtime)
        self.rt.claude = self.rt.codex = self.exe
        self.rt.run = MagicMock(side_effect=AssertionError('posix viewer path used on Windows'))

    def _open(self, call, wt):
        with patch('orchd.runtime.worker_env', side_effect=lambda w: {'ORCHD_WORKTREE': str(Path(w).resolve())}), \
             patch('orchd.runtime.os.name', 'nt'), \
             patch('orchd.runtime.shutil.which', side_effect=lambda name: wt if name == 'wt.exe' else None), \
             patch('orchd.runtime.subprocess.Popen') as popen, \
             patch('orchd.runtime.subprocess.CREATE_NEW_CONSOLE', 0x10, create=True):
            call()
        return popen

    def test_claude_attach_viewer_opens_windows_terminal(self):
        popen = self._open(lambda: self.rt.open_viewer('job-1'), 'wt.exe')
        self.assertEqual(popen.call_args.args[0], ['wt.exe', '-w', 'new', self.exe, 'attach', 'job-1'])

    def test_claude_attach_viewer_falls_back_to_new_console(self):
        popen = self._open(lambda: self.rt.open_viewer('job-1'), None)
        self.assertEqual(popen.call_args.args[0], [self.exe, 'attach', 'job-1'])
        self.assertEqual(popen.call_args.kwargs['creationflags'], 0x10)

    def test_codex_resume_viewer_opens_in_worktree(self):
        popen = self._open(lambda: self.rt.open_codex_viewer(self.tmp.name, 'thread-1'), 'wt.exe')
        self.assertEqual(popen.call_args.args[0],
                         ['wt.exe', '-w', 'new', '-d', self.tmp.name, self.exe, 'resume', 'thread-1'])
        self.assertEqual(popen.call_args.kwargs['cwd'], self.tmp.name)
        # close finds Windows viewers only through the task mark
        self.assertEqual(popen.call_args.kwargs['env']['ORCHD_WORKTREE'], str(Path(self.tmp.name).resolve()))

    def test_claude_task_viewer_carries_the_task_mark(self):
        popen = self._open(lambda: self.rt.open_viewer('job-1', worktree=self.tmp.name), 'wt.exe')
        self.assertEqual(popen.call_args.args[0],
                         ['wt.exe', '-w', 'new', '-d', self.tmp.name, self.exe, 'attach', 'job-1'])
        self.assertEqual(popen.call_args.kwargs['env']['ORCHD_WORKTREE'], str(Path(self.tmp.name).resolve()))

    def test_orch_viewer_has_no_task_mark(self):
        popen = self._open(lambda: self.rt.open_viewer('job-1'), 'wt.exe')
        self.assertIsNone(popen.call_args.kwargs['env'])

    def test_missing_viewer_program_names_the_binary(self):
        self.rt.claude = str(Path(self.tmp.name) / 'missing' / 'claude.exe')
        with self.assertRaisesRegex(FileNotFoundError, 'claude.exe'):
            self._open(lambda: self.rt.open_viewer('job-1'), 'wt.exe')


class ReapWindowsTests(unittest.TestCase):
    """_reap_windows is platform-neutral code driven through fakes, so it runs on every OS."""

    def fake(self, pid, ppid, born=100.0, children=()):
        proc = MagicMock()
        proc.pid, proc.ppid.return_value, proc.create_time.return_value = pid, ppid, born
        proc.children.return_value = list(children)
        return proc

    def run_reap(self, popen_pid, running, processes, since=None):
        import psutil

        def lookup(pid):
            found = [p for p in processes if p.pid == pid]
            if not found:
                raise psutil.NoSuchProcess(pid)
            return found[0]

        popen = MagicMock(pid=popen_pid)
        popen.poll.return_value = None if running else 0
        with patch('psutil.Process', side_effect=lookup), \
                patch('psutil.process_iter', return_value=processes), \
                patch('psutil.wait_procs', side_effect=lambda procs, timeout: ([], [])):
            quota._reap_windows(popen, grace=0, since=since)

    def test_exited_parent_does_not_leave_its_orphan_running(self):
        grandchild = self.fake(31, 30)
        orphan = self.fake(30, 10, children=[grandchild])  # parent pid 10 is already gone
        unrelated = self.fake(99, 1)
        self.run_reap(10, running=False, processes=[orphan, grandchild, unrelated])
        orphan.terminate.assert_called_once()
        grandchild.terminate.assert_called_once()
        unrelated.terminate.assert_not_called()

    def test_pid_reuse_guard_skips_processes_older_than_the_probe(self):
        old = self.fake(30, 10, born=50.0)
        self.run_reap(10, running=False, processes=[old], since=100.0)
        old.terminate.assert_not_called()

    def test_running_parent_is_terminated_with_its_children(self):
        child = self.fake(11, 10)
        root = self.fake(10, 1, children=[child])
        self.run_reap(10, running=True, processes=[root, child])
        root.terminate.assert_called_once()
        child.terminate.assert_called_once()

    def test_reused_root_pid_keeps_the_newcomer_and_its_children_alive(self):
        # Probe root 10 (started at since=100) exited; an unrelated process now holds PID 10 and has a child.
        newcomer_child = self.fake(40, 10, born=210.0)
        newcomer = self.fake(10, 1, born=200.0, children=[newcomer_child])
        orphan = self.fake(30, 10, born=150.0)  # genuinely spawned by the dead probe root
        self.run_reap(10, running=False, processes=[newcomer, newcomer_child, orphan], since=100.0)
        orphan.terminate.assert_called_once()
        newcomer.terminate.assert_not_called()
        newcomer_child.terminate.assert_not_called()

    def test_orphan_scan_rejects_children_born_after_the_root_exit_was_observed(self):
        # The reuser already went away too: only candidates born before the exit was observed can be orphans.
        late = self.fake(41, 10, born=time.time() + 3600)
        orphan = self.fake(30, 10, born=150.0)
        self.run_reap(10, running=False, processes=[late, orphan], since=100.0)
        orphan.terminate.assert_called_once()
        late.terminate.assert_not_called()


class ReapKeepsProbeStartTimeTests(unittest.TestCase):
    """Every cleanup path hands _reap the probe's start time so a reused PID is never killed."""

    def test_reap_children_passes_the_recorded_start_time(self):
        process = MagicMock()
        with quota._children_lock:
            quota._children[process] = 123.5
        self.addCleanup(quota._children.pop, process, None)
        with patch.object(quota, '_reap') as reap:
            quota._reap_children()
        reap.assert_called_once_with(process, since=123.5)

    def test_run_reaps_with_its_start_time_on_failure(self):
        popen = MagicMock()
        popen.communicate.side_effect = KeyboardInterrupt
        with patch('subprocess.Popen', return_value=popen), patch.object(quota, '_reap') as reap:
            with self.assertRaises(KeyboardInterrupt):
                quota._run(['x'], 1)
        self.assertIsNotNone(reap.call_args.kwargs['since'])
        self.assertNotIn(popen, quota._children)

    def _probe_app_server(self, started, popen_factory):
        cache = MagicMock()
        cache.status.return_value = {'sol': {'windows': []}}
        with patch('orchd.quota.time.time', return_value=started), \
                patch('orchd.quota.subprocess.Popen', side_effect=popen_factory) as popen, \
                patch.object(quota, '_reap') as reap:
            quota._probe(cache, 'sol', binary='codex')
        return popen.call_args, reap

    def test_app_server_probe_cleanup_passes_the_recorded_start_time_when_the_server_exits(self):
        process = MagicMock()
        process.poll.return_value = 0  # app-server died before publishing its socket
        _, reap = self._probe_app_server(777.5, lambda *a, **k: process)
        reap.assert_called_once_with(process, since=777.5)
        self.assertNotIn(process, quota._children)

    def test_app_server_probe_cleanup_passes_the_recorded_start_time_after_a_read(self):
        process = MagicMock()
        process.poll.return_value = None

        def popen(cmd, **kwargs):
            Path(cmd[-1].removeprefix('unix://')).write_text('')  # the server published its socket
            return process

        with patch('orchd.app_transport.RPC') as rpc:
            rpc.return_value.call.return_value = {'rate_limits': {}}
            call_args, reap = self._probe_app_server(888.25, popen)
        self.assertEqual(call_args.args[0][:2], ['codex', 'app-server'])
        rpc.return_value.close.assert_called_once()
        reap.assert_called_once_with(process, since=888.25)
        self.assertNotIn(process, quota._children)
