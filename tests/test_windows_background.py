import os
import subprocess
import unittest
from unittest.mock import patch

from orchd import platform_process
from orchd.runtime import Runtime


class BackgroundOptionsTests(unittest.TestCase):
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

    @unittest.skipUnless(os.name == 'nt', 'Windows viewer')
    def test_windows_viewer_opens_new_console_when_terminal_unavailable(self):
        task = {'worktree': os.getcwd(), 'id': 'task', 'generation': 'gen'}
        with patch('orchd.runtime.worker_env', return_value={}), patch('orchd.runtime.shutil.which', return_value=None), \
             patch('orchd.runtime.subprocess.Popen') as popen:
            Runtime().open_app_viewer('db', task)
        self.assertEqual(popen.call_args.kwargs['creationflags'], subprocess.CREATE_NEW_CONSOLE)
        self.assertEqual(popen.call_args.args[0][-4:], ['viewer', 'db', 'task', 'gen'])
