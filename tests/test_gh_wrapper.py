"""Exercise the executable boundary, including git's actual credential protocol."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from orchd.runtime import Runtime, worker_env
from orchd.core import worker_brief

WRAPPER = Path(__file__).resolve().parents[1] / "bin" / "worker-bin"


class GhWrapperTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.fake = self.root / "bin"
        self.fake.mkdir()
        gh = self.fake / "gh"
        gh.write_text('''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with open(os.environ['CALLS'], 'a') as f:
    f.write(json.dumps(args) + '\\n')
if args[:2] == ['auth', 'token']:
    account = args[args.index('-u') + 1]
    if account == 'missing': sys.exit(1)
    print('token-' + account)
elif args[:2] == ['auth', 'git-credential']:
    data = sys.stdin.read()
    Path(os.environ['INPUT']).write_text(data)
    print('username=selected\\npassword=' + os.environ.get('GH_TOKEN', 'original') + '\\n')
else:
    print(json.dumps({'token': os.environ.get('GH_TOKEN'), 'args': args,
                      'active': Path(os.environ['ACTIVE']).read_text()}))
''')
        gh.chmod(0o755)
        self.active = self.root / "active"
        self.active.write_text('ariontechs')
        self.env = dict(os.environ, PATH=f'{WRAPPER}:{self.fake}:' + os.environ['PATH'],
                        CALLS=str(self.root / 'calls'), INPUT=str(self.root / 'input'), ACTIVE=str(self.active))
        for key in ('GH_TOKEN', 'GH_REPO', 'ORCHD_GH_ACCOUNTS'):
            self.env.pop(key, None)
        subprocess.run(['git', 'init', '-q', str(self.root / 'repo')], check=True)
        self.repo = self.root / 'repo'
        subprocess.run(['git', 'remote', 'add', 'origin', 'git@github-NatChung:NatChung/orchd.git'],
                       cwd=self.repo, check=True)

    def run_gh(self, *args, input=None, env=None, cwd=None):
        return subprocess.run(['gh', *args], env=env or self.env, cwd=cwd or self.repo,
                              input=input, capture_output=True, text=True, timeout=10, check=True)

    def test_repo_sources_and_precedence(self):
        for args, extra, token in [
            (['issue', 'view', '37', '--repo', 'NatChung/orchd'], {'GH_REPO': 'Other/repo'}, 'NatChung'),
            (['repo', 'view', '-R', 'Other/repo'], {}, 'ariontechs'),
            (['repo', 'view', '--repo=NatChung/orchd'], {}, 'NatChung'),
            (['repo', 'view', '-RNatChung/orchd'], {}, 'NatChung'),
            (['repo', 'view'], {'GH_REPO': 'Other/repo'}, 'ariontechs'),
            (['repo', 'view'], {'GH_REPO': 'NatChung/orchd'}, 'NatChung'),
            (['repo', 'view'], {}, 'NatChung'),
        ]:
            with self.subTest(args=args, extra=extra):
                result = json.loads(self.run_gh(*args, env=dict(self.env, **extra)).stdout)
                self.assertEqual(result['token'], 'token-' + token)
                self.assertEqual(result['args'], args)

    def test_existing_token_is_preserved_even_empty(self):
        for token in ('user-token', ''):
            result = json.loads(self.run_gh('repo', 'view', env=dict(self.env, GH_TOKEN=token)).stdout)
            self.assertEqual(result['token'], token)
        calls = (self.root / 'calls').read_text()
        self.assertNotIn('"token"', calls)

    def test_missing_mapping_and_token_fall_back(self):
        config = self.root / 'accounts.json'
        for mapping, warning in [({'Other': 'ariontechs'}, 'no account mapping'),
                                 ({'NatChung': 'missing'}, 'token unavailable'),
                                 ({'natchung': 'custom'}, None)]:
            config.write_text(json.dumps(mapping))
            result = self.run_gh('repo', 'view', env=dict(self.env, ORCHD_GH_ACCOUNTS=str(config)))
            self.assertEqual(json.loads(result.stdout)['token'], 'token-custom' if warning is None else None)
            if warning:
                self.assertEqual(len(result.stderr.splitlines()), 1)
                self.assertIn(warning, result.stderr)

    def test_no_recursion_with_duplicate_and_symlink_path_entries(self):
        alias = self.root / 'alias'
        alias.mkdir()
        (alias / 'gh').symlink_to(WRAPPER / 'gh')
        env = dict(self.env, PATH=f'{WRAPPER}:{alias}:' + self.env['PATH'])
        self.assertEqual(json.loads(self.run_gh('repo', 'view', env=env).stdout)['token'], 'token-NatChung')
        self.assertEqual(len((self.root / 'calls').read_text().splitlines()), 2)
        result = subprocess.run([str(WRAPPER / 'gh')], env=dict(self.env, PATH=f'{WRAPPER}:{alias}:/usr/bin'),
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 127)
        self.assertIn('no real gh', result.stderr)

    def test_git_credential_uses_remote_over_env_and_origin(self):
        for owner, token in [('NatChung', 'NatChung'), ('Other', 'ariontechs')]:
            request = f'protocol=https\nhost=github.com\npath={owner}/repo.git\n\n'
            result = self.run_gh('auth', 'git-credential', 'get', input=request,
                                 env=dict(self.env, GH_REPO='Ignored/repo'))
            self.assertIn('password=token-' + token, result.stdout)
            self.assertEqual((self.root / 'input').read_text(), request)
        # Older git helpers without a path still use the current origin.
        result = self.run_gh('auth', 'git-credential', 'get', input='protocol=https\nhost=github.com\n\n')
        self.assertIn('password=token-NatChung', result.stdout)

    def test_git_uses_wrapper_over_absolute_existing_helper(self):
        with patch.dict(os.environ, self.env, clear=True):
            env = worker_env()
        request = 'protocol=https\nhost=github.com\npath=NatChung/repo.git\n\n'
        result = subprocess.run(['git', '-c', 'credential.helper=/nonexistent/gh auth git-credential',
                                 'credential', 'fill'], env=env, cwd=self.repo, input=request,
                                capture_output=True, text=True, timeout=10, check=True)
        self.assertIn('password=token-NatChung', result.stdout)
        self.assertNotIn('nonexistent', result.stderr)

    def test_auth_status_and_global_account_are_unchanged(self):
        self.run_gh('repo', 'view')
        result = json.loads(self.run_gh('auth', 'status').stdout)
        self.assertIsNone(result['token'])
        self.assertEqual(result['active'], 'ariontechs')
        self.assertEqual(self.active.read_text(), 'ariontechs')
        self.assertNotIn('switch', (self.root / 'calls').read_text())

    def test_claude_settings_and_codex_spawn_receive_worker_path(self):
        rt = Runtime()
        seen = {}
        rt.run = lambda cmd, **kw: seen.update(cmd=cmd, **kw) or subprocess.CompletedProcess(cmd, 0, 'claude attach j1', '')
        rt.agents = lambda: [{'id': 'j1', 'sessionId': 's1'}]
        rt.exists = lambda path: True
        with patch.dict(os.environ, self.env, clear=True):
            rt.start_worker('/wt', '/tmp/s', 'brief', 'claude-sonnet-5-5')
            settings = json.loads(seen['cmd'][seen['cmd'].index('--settings') + 1])
            self.assertEqual(settings['env']['PATH'].split(os.pathsep)[0], str(WRAPPER))
            self.assertEqual(settings['crossSessionInbound'], 'accept')
            with patch('orchd.runtime.subprocess.Popen') as popen:
                rt.spawn(['codex', 'exec'], str(self.repo), str(self.root / 'log'))
                env = popen.call_args.kwargs['env']
                self.assertEqual(env['PATH'].split(os.pathsep)[0], str(WRAPPER))
                self.assertEqual(env['GIT_CONFIG_COUNT'], '3')
        self.assertIn('Read-only gh commands need no account switch and no ask', worker_brief('orchd'))
        self.assertIn('Before any outward send', worker_brief('orchd'))

    def test_session_git_config_preserves_parent_entries(self):
        with patch.dict(os.environ, dict(self.env, GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0='test.key',
                                        GIT_CONFIG_VALUE_0='parent'), clear=True):
            env = worker_env()
        self.assertEqual(env['GIT_CONFIG_COUNT'], '4')
        self.assertEqual(env['GIT_CONFIG_VALUE_0'], 'parent')
        self.assertEqual(env['GIT_CONFIG_KEY_1'], 'credential.https://github.com.helper')
