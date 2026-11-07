import os
import subprocess
import tempfile
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
