"""Project locations stay consistent across doctor, dispatch and background MCP."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchd import doctor, paths, runtime


class ProjectPathsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.config = self.home / '.config' / 'orchd' / 'config.toml'
        self.config.parent.mkdir(parents=True)

    def test_installed_fallback_is_target_home_not_cwd(self):
        with mock.patch('orchd.paths.__file__', '/opt/tools/orchd/paths.py'):
            self.assertEqual(paths.projects_dir(self.home, {}), self.home / 'projects')

    def test_env_wins_over_config_and_expands_target_home(self):
        self.config.write_text('projects_dir = "/other"\n')
        self.assertEqual(paths.projects_dir(self.home, {'ORCHD_PROJECTS': '~/GitProjects'}),
                         self.home / 'GitProjects')

    def test_config_root_override_and_absolute_projects(self):
        self.config.write_text('projects_dir = "/somewhere/Git Projects"\n')
        self.assertEqual(paths.projects_dir(self.home, {'ORCHD_CONFIG_DIR': str(self.config.parent)}),
                         Path('/somewhere/Git Projects'))

    def test_bad_config_fails_doctor_without_crashing_or_silent_fallback(self):
        for value in ('projects_dir = [', 'projects_dir = 7', 'projects_dir = ""',
                      'projects_dir = "relative/path"'):
            with self.subTest(value=value):
                self.config.write_text(value)
                d = doctor.Doctor(home=self.home, env={})
                d.check_repos_trust()
                self.assertEqual(doctor.exit_code(d.checks), 1)
                with self.assertRaises(ValueError):
                    paths.projects_dir(self.home, {})

    def test_doctor_dispatch_and_claude_mcp_use_same_config_from_any_cwd(self):
        root = self.home / 'Git Projects'
        (root / 'demo' / '.git').mkdir(parents=True)
        self.config.write_text(f'projects_dir = "{root}"\n')
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(Path, 'home', return_value=self.home):
            d = doctor.Doctor()
            rt = runtime.Runtime()
            self.assertEqual(d.projects, root)
            self.assertEqual(rt.repo_path('demo'), (root / 'demo').resolve())
            self.assertEqual(runtime.mcp_env('orch1')['ORCHD_PROJECTS'], str(root))
