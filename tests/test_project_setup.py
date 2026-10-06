import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchd import cli, doctor, paths


class ProjectSetupTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.projects = self.home / 'My Repositories'
        self.projects.mkdir()
        self.config = self.home / '.config' / 'orchd' / 'config.toml'
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.dict(os.environ, {}, clear=True))
        self.stack.enter_context(mock.patch.object(Path, 'home', return_value=self.home))
        self.stack.enter_context(mock.patch('orchd.paths.__file__', '/opt/tools/orchd/paths.py'))
        self.stack.enter_context(mock.patch.object(doctor.Doctor, 'run_all', return_value=[]))
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))

    def test_first_doctor_prompts_and_remembers_custom_directory(self):
        with mock.patch.object(cli.sys.stdin, 'isatty', return_value=True), mock.patch('builtins.input', return_value=str(self.projects)) as ask:
            self.assertEqual(cli.main(['doctor']), 0)
            self.assertEqual(paths.projects_dir(), self.projects)
            cli.main(['doctor'])
        ask.assert_called_once()

    def test_json_never_prompts_or_writes(self):
        with mock.patch.object(cli.sys.stdin, 'isatty', return_value=True), mock.patch('builtins.input') as ask:
            cli.main(['doctor', '--json'])
        ask.assert_not_called()
        self.assertFalse(self.config.exists())

    def test_no_input_keeps_doctor_read_only(self):
        with mock.patch.object(cli.sys.stdin, 'isatty', return_value=True), mock.patch('builtins.input') as ask:
            cli.main(['doctor', '--no-input'])
        ask.assert_not_called()
        self.assertFalse(self.config.exists())

    def test_preserves_existing_tables_and_comments(self):
        self.config.parent.mkdir(parents=True)
        original = '# keep this comment\n[other]\nvalue = "saved"\n'
        self.config.write_text(original)
        with mock.patch.object(cli.sys.stdin, 'isatty', return_value=True), mock.patch('builtins.input', return_value=str(self.projects)):
            cli.main(['doctor'])
        self.assertTrue(self.config.read_text().endswith(original))
        self.assertEqual(paths.projects_dir(), self.projects)

    def test_invalid_directory_reprompts_and_expands_tilde(self):
        with mock.patch.object(cli.sys.stdin, 'isatty', return_value=True), mock.patch('builtins.input', side_effect=['relative', '/does/not/exist', '~/My Repositories']) as ask:
            cli.main(['doctor'])
        self.assertEqual(ask.call_count, 3)
        self.assertEqual(paths.projects_dir(), self.projects)

    def test_skip_eof_and_cancel_leave_no_config(self):
        for answer in ('', EOFError(), KeyboardInterrupt()):
            with self.subTest(answer=answer):
                kwargs = {'side_effect': answer} if isinstance(answer, BaseException) else {'return_value': answer}
                with mock.patch.object(cli.sys.stdin, 'isatty', return_value=True), mock.patch('builtins.input', **kwargs):
                    cli.main(['doctor'])
                self.assertFalse(self.config.exists())

    def test_noninteractive_run_never_prompts(self):
        with mock.patch.object(cli.sys.stdin, 'isatty', return_value=False), mock.patch('builtins.input') as ask:
            cli.main(['doctor'])
        ask.assert_not_called()
        self.assertFalse(self.config.exists())

    def test_existing_default_or_explicit_override_never_prompts(self):
        with mock.patch.object(cli.sys.stdin, 'isatty', return_value=True), mock.patch('builtins.input') as ask:
            (self.home / 'projects').mkdir()
            cli.main(['doctor'])
            (self.home / 'projects').rmdir()
            with mock.patch.dict(os.environ, {'ORCHD_PROJECTS': '/configured-but-missing'}):
                cli.main(['doctor'])
        ask.assert_not_called()
        self.assertFalse(self.config.exists())

    def test_bad_config_is_reported_by_doctor_without_overwrite(self):
        self.config.parent.mkdir(parents=True)
        original = 'projects_dir = ['
        self.config.write_text(original)
        with mock.patch.object(cli.sys.stdin, 'isatty', return_value=True), mock.patch('builtins.input') as ask:
            cli.main(['doctor'])
        ask.assert_not_called()
        self.assertEqual(self.config.read_text(), original)
