import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchd import config, core, worker_flow


class WorkerFlowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / 'config.toml'
        self.env = patch.dict(os.environ, {'ORCHD_CONFIG_FILE': str(self.config)})
        self.env.start()
        self.addCleanup(self.env.stop)
        worker_flow._fetches.clear()
        self.git('init', '-q', '--bare', str(self.root / 'adapter.git'))
        self.git('clone', '-q', str(self.root / 'adapter.git'), str(self.root / 'orchd'))
        self.commit_file(self.root / 'orchd', 'docs/worker-flow-adapter.md')
        self.git('-C', str(self.root / 'orchd'), 'push', '-q', 'origin', 'HEAD')
        self.git('-C', str(self.root / 'orchd'), 'remote', 'set-head', 'origin', '-a')
        self.git('init', '-q', '--bare', str(self.root / 'remote.git'))
        self.git('clone', '-q', str(self.root / 'remote.git'), str(self.root / 'hub'))
        self.commit_file(self.root / 'hub', 'docs/development/delivery.md')
        self.git('-C', str(self.root / 'hub'), 'push', '-q', 'origin', 'HEAD')
        self.git('-C', str(self.root / 'hub'), 'remote', 'set-head', 'origin', '-a')

    def git(self, *args):
        return subprocess.run(['git', *args], check=True, capture_output=True, text=True).stdout.strip()

    def commit_file(self, repo, name):
        file = repo / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text('flow contents')
        self.git('-C', str(repo), 'add', name)
        self.git('-C', str(repo), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                 'commit', '-qm', 'fixture')

    def test_configured_flow_for_both_backends(self):
        self.config.write_text('[delivery_flows]\nproduct = "hub"\n')
        sha = self.git('-C', str(self.root / 'hub'), 'rev-parse', 'HEAD')
        adapter_sha = self.git('-C', str(self.root / 'orchd'), 'rev-parse', 'HEAD')
        for kind in ('claude', 'codex'):
            with self.subTest(kind=kind):
                brief = core.worker_brief('orchd', kind, repo='product', projects=self.root)
                self.assertIn(f'origin SHA {sha}', brief)
                self.assertIn(f'orchd SHA {adapter_sha}', brief)
                self.assertIn(f'{sha}:docs/development/delivery.md', brief)
                self.assertIn('git -C', brief)
        self.assertIn(sha, core.app_brief(repo='product', projects=self.root))

    def test_unconfigured_only_has_adapter(self):
        brief = core.worker_brief('orchd', repo='product', projects=self.root)
        self.assertIn('Worker flow adapter:', brief)
        self.assertNotIn('Delivery flow:', brief)

    def test_missing_hub_and_failed_fetch_are_visible(self):
        self.config.write_text('[delivery_flows]\nproduct = "absent"\n')
        self.assertIn('Delivery flow: missing', core.worker_brief('orchd', repo='product', projects=self.root))
        self.config.write_text('[delivery_flows]\nproduct = "hub"\n')
        worker_flow._fetches.clear()
        self.git('-C', str(self.root / 'hub'), 'remote', 'set-url', 'origin', str(self.root / 'absent'))
        self.assertIn('Delivery flow: missing', core.worker_brief('orchd', repo='product', projects=self.root))

    def test_missing_entry_is_visible(self):
        self.config.write_text('[delivery_flows]\nproduct = "hub"\n')
        self.git('-C', str(self.root / 'hub'), 'rm', '-q', 'docs/development/delivery.md')
        self.git('-C', str(self.root / 'hub'), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                 'commit', '-qm', 'remove')
        self.git('-C', str(self.root / 'hub'), 'push', '-q', 'origin', 'HEAD')
        self.assertIn('Delivery flow: missing', core.worker_brief('orchd', repo='product', projects=self.root))

    def test_config_validation_and_default(self):
        self.assertEqual(config.delivery_flows(), {})
        for text in ('delivery_flows = "bad"', '[delivery_flows]\n"../product" = "hub"',
                     '[delivery_flows]\nproduct = "../hub"', '[delivery_flows]\nproduct = 42', '[delivery_flows]\n"" = "hub"',
                     '[delivery_flows]\nproduct = "path\\\\hub"'):
            with self.subTest(text=text):
                self.config.write_text(text)
                with self.assertRaises(ValueError):
                    config.delivery_flows()

    def test_fetch_deduplicated_and_retry_resolves_again(self):
        self.config.write_text('[delivery_flows]\nproduct = "hub"\n')
        original = worker_flow._git
        with patch.object(worker_flow, '_git', wraps=original) as git:
            for _ in range(4):
                brief = worker_flow.brief('product', self.root)
            fetches = [c for c in git.call_args_list if c.args[2:] == ('fetch', '--quiet', 'origin')]
            self.assertEqual(len(fetches), 2)
            self.assertIn('a retry may resolve different SHAs', brief)
            self.assertEqual(sum(c.args[2] == 'rev-parse' for c in git.call_args_list), 8)

    def test_timeout_budget_and_missing(self):
        self.config.write_text('[delivery_flows]\nproduct = "hub"\n')
        clock = [0.0]
        def slow(*args, **kwargs):
            timeout = kwargs['timeout']
            clock[0] += timeout
            self.assertEqual(kwargs['env']['GIT_TERMINAL_PROMPT'], '0')
            raise subprocess.TimeoutExpired(args[0], timeout, stderr='timed out')
        with patch.object(worker_flow.time, 'monotonic', side_effect=lambda: clock[0]), \
             patch.object(worker_flow.subprocess, 'run', side_effect=slow):
            brief = worker_flow.brief('product', self.root)
        self.assertIn('Worker flow adapter: missing', brief)
        self.assertIn('Delivery flow: missing', brief)
        self.assertLessEqual(clock[0], worker_flow.BRIEF_BUDGET)
        self.assertLess(worker_flow.BRIEF_BUDGET, core.RETRY_LOCK_WAIT)

    def test_error_redacts_credentials(self):
        error = subprocess.CalledProcessError(1, ['git'], stderr='fatal https://user:secret@example.com/path')
        with patch.object(worker_flow, '_git', side_effect=error):
            brief = worker_flow.brief(None, self.root)
        self.assertIn('fatal https://***@example.com/path', brief)
        self.assertNotIn('secret', brief)
