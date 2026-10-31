"""Project locations stay consistent across doctor, dispatch and background MCP."""
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchd import core, doctor, paths, runtime


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


class OrchdCommandTest(unittest.TestCase):
    """Internal paths must not run bin/orchd under whatever `python3` is first on PATH (it may lack psutil)."""

    def script(self, first_line):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / 'orchd'
        path.write_text(first_line + '\n')
        return str(path)

    def test_env_python3_script_runs_under_this_interpreter(self):
        exe = self.script('#!/usr/bin/env python3')
        self.assertEqual(paths.orchd_command({'ORCHD_EXECUTABLE': exe}), [sys.executable, exe])

    def test_checkout_script_runs_under_this_interpreter(self):
        checkout = Path(paths.__file__).resolve().parents[1] / 'bin' / 'orchd'
        self.assertEqual(paths.orchd_command({}), [sys.executable, str(checkout)])

    def test_non_python_override_is_launched_directly(self):
        exe = self.script('#!/bin/sh')
        self.assertEqual(paths.orchd_command({'ORCHD_EXECUTABLE': exe}), [exe])

    def test_installed_entry_point_with_its_own_interpreter_is_launched_directly(self):
        exe = self.script('#!/opt/venv/bin/python')
        self.assertEqual(paths.orchd_command({'ORCHD_EXECUTABLE': exe}), [exe])

    def test_worker_cli_hook_and_codex_turn_use_the_interpreter(self):
        exe = self.script('#!/usr/bin/env python3')
        with mock.patch.dict(os.environ, {'ORCHD_EXECUTABLE': exe}):
            self.assertTrue(core.worker_cli().startswith(shlex.join([sys.executable, exe])))
            turn = runtime.Runtime().codex_turn(['codex'], '/l/orchd-abc123/codex.jsonl')
            self.assertIn(shlex.join([sys.executable, exe]) + ' flush', turn[2])
            self.assertIn(shlex.join([sys.executable, exe]) + ' quota', turn[2])
            rt = runtime.Runtime()
            seen = {}
            rt.run = lambda cmd, **kw: seen.update(cmd=cmd) or subprocess.CompletedProcess(cmd, 0, 'claude attach j1', '')
            rt.agents = lambda: [{'id': 'j1', 'sessionId': 's1'}]
            rt.exists = lambda path: True
            rt.start_worker('/wt', '/tmp/s', 'brief', 'claude-sonnet-5-5')
            settings = json.loads(seen['cmd'][seen['cmd'].index('--settings') + 1])
            hook = settings['hooks']['Stop'][0]['hooks'][0]['command']
            self.assertIn(shlex.join([sys.executable, exe, 'quota']), hook)
