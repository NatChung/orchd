import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchd import core


class WorkerFlowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / 'config.toml'
        self.env = patch.dict(os.environ, {'ORCHD_CONFIG_FILE': str(self.config)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.git('init', '-q', str(self.root / 'orchd'))
        self.commit_file(self.root / 'orchd', 'docs/worker-flow-adapter.md')
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
        self.git('-C', str(self.root / 'hub'), 'remote', 'set-url', 'origin', str(self.root / 'absent'))
        self.assertIn('Delivery flow: missing', core.worker_brief('orchd', repo='product', projects=self.root))

    def test_missing_entry_is_visible(self):
        self.config.write_text('[delivery_flows]\nproduct = "hub"\n')
        self.git('-C', str(self.root / 'hub'), 'rm', '-q', 'docs/development/delivery.md')
        self.git('-C', str(self.root / 'hub'), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                 'commit', '-qm', 'remove')
        self.git('-C', str(self.root / 'hub'), 'push', '-q', 'origin', 'HEAD')
        self.assertIn('Delivery flow: missing', core.worker_brief('orchd', repo='product', projects=self.root))
