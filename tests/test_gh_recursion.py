"""In-process regression coverage for worker gh resolution and recursion."""
import os
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest.mock import patch

WRAPPER = Path(__file__).resolve().parents[1] / 'orchd' / 'worker_bin' / 'gh'


class WrapperResolutionTest(unittest.TestCase):
    def setUp(self):
        self.wrapper = runpy.run_path(str(WRAPPER))
        self.globals = self.wrapper['real_gh'].__globals__
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.own = self.root / 'wrapper'
        self.real = self.root / 'installed'
        self.own.mkdir()
        self.real.mkdir()
        (self.own / 'gh').write_text('wrapper')
        (self.own / 'gh.CMD').write_text('shim')
        (self.real / 'gh.EXE').write_text('real')

    @unittest.skipUnless(os.name == 'nt', 'Windows PATHEXT resolution')
    def test_windows_skips_own_directory_with_or_without_environment_hint(self):
        for hint in (None, str(self.own).swapcase()):
            with self.subTest(hint=hint):
                env = {'PATH': os.pathsep.join((str(self.own).swapcase(), str(self.real))), 'PATHEXT': '.CMD;.EXE'}
                if hint:
                    env['ORCHD_GH_WRAPPER_DIR'] = hint
                with patch.dict(os.environ, env, clear=True), patch.dict(self.globals, {'__file__': str(self.own / 'gh')}):
                    self.assertEqual(Path(self.wrapper['real_gh']()).resolve(), (self.real / 'gh.EXE').resolve())

    def test_existing_posix_search_skips_duplicate_wrapper(self):
        duplicate = self.real / 'gh'
        duplicate.write_text('wrapper')
        candidate = self.root / 'real-gh'
        candidate.write_text('real')
        with patch.dict(os.environ, {'PATH': 'first' + os.pathsep + 'second'}, clear=True), \
             patch.dict(self.globals, {'__file__': str(self.own / 'gh')}), \
             patch.object(self.globals['shutil'], 'which', side_effect=[str(duplicate), str(candidate)]):
            self.assertEqual(self.wrapper['real_gh'](), str(candidate))

    def test_guard_refuses_before_search_or_subprocess(self):
        for value in ('1', ''):
            with self.subTest(value=value), patch.dict(os.environ, {'ORCHD_GH_WRAPPER_ACTIVE': value}, clear=True), \
                 patch.dict(self.globals, {'real_gh': unittest.mock.Mock(side_effect=AssertionError('searched'))}):
                with patch('sys.stderr') as stderr:
                    self.assertNotEqual(self.wrapper['main'](), 0)
                    self.assertIn('recursion', ''.join(str(call) for call in stderr.write.call_args_list).lower())

    @unittest.skipUnless(os.name == 'nt', 'Windows child process')
    def test_marker_is_set_for_child_process(self):
        with patch.dict(os.environ, {'GH_TOKEN': 'fake'}, clear=True), \
             patch.dict(self.globals, {'real_gh': lambda: 'fake.exe', 'gate_pr_create': lambda args: (0, args)}), \
             patch.object(self.globals['subprocess'], 'call', return_value=0) as call:
            self.assertEqual(self.wrapper['main'](), 0)
            self.assertEqual(os.environ['ORCHD_GH_WRAPPER_ACTIVE'], '1')
            call.assert_called_once()

    @unittest.skipUnless(os.name == 'nt', 'Windows child process')
    def test_nested_credential_helper_is_allowed_and_cannot_recurse(self):
        import io
        from types import SimpleNamespace

        stdin = SimpleNamespace(buffer=io.BytesIO(b'host=github.com\n'))
        with patch.dict(os.environ, {'ORCHD_GH_WRAPPER_ACTIVE': '1', 'ORCHD_CONFIG_DIR': str(self.root)}, clear=True), \
             patch.dict(self.globals, {'real_gh': lambda: 'fake.exe'}), \
             patch('sys.argv', ['gh', 'auth', 'git-credential', 'get']), \
             patch('sys.stdin', stdin), \
             patch.object(self.globals['subprocess'], 'call', return_value=0) as call:
            self.assertEqual(self.wrapper['main'](), 0)
            call.assert_called_once()
            self.assertNotEqual(self.wrapper['main'](), 0)
