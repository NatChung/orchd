import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from orchd import bootstrap, cli, instances, paths, store, workflows


class InstanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name).resolve()
        self.repo, self.root, self.projects = [self.folder / name for name in ('example-workflow', 'instance', 'projects')]
        self.repo.mkdir()
        self.projects.mkdir()
        environment = patch.dict(os.environ, {'ORCHD_CONFIG_DIR': str(self.folder / 'caller-config'),
                                              'ORCHD_HOME': str(self.folder / 'caller-state'),
                                              'ORCHD_ORCH_HOME': str(self.folder / 'caller-orch-home')})
        environment.start()
        self.addCleanup(environment.stop)
        self.git('init', '-q')
        workflow = {'schema_version': 'orchd-workflow/v1', 'name': 'example-workflow', 'description': 'Synthetic workflow',
                    'stages': {'inspect': {'task_type': 'investigation', 'inputs': [],
                                           'instructions_file': 'inspect.md', 'done_when_file': 'done.md'}}}
        manifest = {'schema_version': 'orchd-instance/v1', 'name': 'example-instance', 'workflow': 'example-workflow',
                    'agents_file': 'profile/AGENTS.local.md', 'projects_file': 'profile/PROJECTS.md'}
        self.write('orchd-workflow.json', workflows.canonical(workflow))
        self.write('orchd-instance.json', workflows.canonical(manifest))
        self.write('inspect.md', b'Read the caller request.\n')
        self.write('done.md', b'Return evidence.\n')
        self.write('profile/AGENTS.local.md', b'Follow the installed workflow and preserve runtime permissions.\n')
        self.write('profile/PROJECTS.md', b'# Projects\n\n- reminder-service\n')
        self.commit = self.record()

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], stderr=subprocess.PIPE).decode().strip()

    def write(self, name, raw):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)

    def record(self):
        self.git('add', '-A')
        self.git('-c', 'user.name=Example Maintainer', '-c', 'user.email=maintainer@example.invalid',
                 'commit', '-qm', 'Record synthetic instance settings')
        return self.git('rev-parse', 'HEAD')

    def apply(self, **kwargs):
        return instances.apply(self.repo, self.commit, self.root, self.projects, **kwargs)

    def snapshot(self):
        return {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}

    def test_fresh_check_is_write_free_apply_is_scoped_and_idempotent_without_task_db_or_process(self):
        with patch.object(store, 'connect') as database, patch('orchd.runtime.Runtime.start_orch') as start:
            checked = self.apply(check=True)
            self.assertFalse(self.root.exists())
            self.assertEqual(checked['restart_required'], ['orch', 'interface'])
            result = self.apply()
            self.assertEqual(result['status'], 'instance_applied')
            self.assertFalse((self.folder / 'caller-config').exists())
            self.assertFalse((self.folder / 'caller-state').exists())
            receipt, digest = instances.read_receipt(self.root)
            self.assertEqual(receipt['source_repo'], str(self.repo))
            self.assertEqual(receipt['source_commit'], self.commit)
            self.assertEqual(result['receipt_digest'], digest)
            self.assertFalse(list(self.root.rglob('*.db')))
            before = self.snapshot()
            repeated = self.apply()
            self.assertEqual(repeated['changed_files'], [])
            self.assertEqual(self.snapshot(), before)
            with instances.profile(receipt['profile']):
                self.assertEqual(workflows.bindings()['default'], 'example-workflow')
            database.assert_not_called()
            start.assert_not_called()

    def test_new_commit_updates_owned_section_and_preserves_managed_block_and_local_notes(self):
        self.apply()
        path = self.root / 'home/AGENTS.md'
        original = path.read_bytes()
        path.write_bytes(b'Local notes before.\n' + original + b'Local notes after.\n')
        self.write('profile/AGENTS.local.md', b'New committed workflow guidance.\n')
        self.commit = self.record()
        result = self.apply()
        self.assertEqual(result['restart_required'], ['orch'])
        actual = path.read_bytes()
        self.assertTrue(actual.startswith(b'Local notes before.\n'))
        self.assertTrue(actual.endswith(b'Local notes after.\n'))
        before_start, before_end = instances.block(original)
        after_start, after_end = instances.block(actual)
        self.assertEqual(actual[len(b'Local notes before.\n'):after_start], original[:before_start])
        self.assertEqual(actual[after_end:-len(b'Local notes after.\n')], original[before_end:])
        self.assertIn(b'New committed workflow guidance.', actual)

    def test_drift_and_stale_receipt_reject_without_any_file_changes(self):
        self.apply()
        _, digest = instances.read_receipt(self.root)
        projects = self.root / 'home/PROJECTS.md'
        projects.write_text('Manual local change')
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, 'drift'):
            self.apply()
        self.assertEqual(self.snapshot(), before)
        projects.write_bytes(b'# Projects\n\n- reminder-service\n')
        with self.assertRaisesRegex(ValueError, 'receipt changed'):
            self.apply(expected_receipt='sha256:' + '0' * 64)
        self.assertEqual(instances.read_receipt(self.root)[1], digest)
        agents = self.root / 'home/AGENTS.md'
        agents.write_bytes(agents.read_bytes().replace(b'Follow the installed workflow', b'Manually changed guidance'))
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, 'section drift'):
            self.apply()
        self.assertEqual(self.snapshot(), before)

    def test_existing_unknown_files_are_not_adopted_but_exact_bootstrap_files_are(self):
        self.root.mkdir()
        agents = self.root / 'home/AGENTS.md'
        agents.parent.mkdir()
        agents.write_text('Unknown rules')
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, 'adopt'):
            self.apply()
        self.assertEqual(self.snapshot(), before)
        env = dict(ORCHD_ROOT=str(self.root), ORCHD_HOME=str(self.root / 'state'),
                   ORCHD_CONFIG_DIR=str(self.root / 'config'), ORCHD_PROJECTS=str(self.projects),
                   ORCHD_EXECUTABLE=str(paths.orchd_executable()))
        agents.write_text(bootstrap.planned_files(Path.home(), env)[agents])
        self.apply()
        self.assertTrue((self.root / instances.RECEIPT).is_file())

    def test_symlink_source_target_overlap_and_unsafe_documents_are_rejected(self):
        self.root.symlink_to(self.folder / 'outside')
        with self.assertRaisesRegex(ValueError, 'symlink'):
            self.apply()
        self.root.unlink()
        with self.assertRaisesRegex(ValueError, 'ignored'):
            instances.apply(self.repo, self.commit, self.repo / 'output', self.projects)
        document = self.repo / 'profile/AGENTS.local.md'
        document.unlink()
        document.symlink_to('PROJECTS.md')
        self.commit = self.record()
        with self.assertRaisesRegex(ValueError, 'regular committed'):
            self.apply()
        self.assertFalse(self.root.exists())

    def test_source_local_runtime_requires_ignored_untracked_subtree_and_excludes_git_metadata(self):
        self.write('.gitignore', b'.runtime/\n')
        self.commit = self.record()
        runtime = self.repo / '.runtime'
        result = instances.apply(self.repo, self.commit, runtime, self.projects)
        self.assertEqual(result['status'], 'instance_applied')
        self.assertEqual(self.git('status', '--porcelain'), '')
        with self.assertRaisesRegex(ValueError, 'Git metadata'):
            instances.apply(self.repo, self.commit, self.repo / '.git/runtime', self.projects)
        tracked = self.repo / 'tracked-runtime'
        self.write('tracked-runtime/keep.md', b'Keep tracked content')
        self.commit = self.record()
        with self.assertRaisesRegex(ValueError, 'tracked files'):
            instances.apply(self.repo, self.commit, tracked, self.projects)

    def test_cli_instance_operations_skip_home_migrations_and_enforce_operator_context(self):
        args = ['instance', 'check', str(self.repo), '--commit', self.commit,
                '--root', str(self.root), '--projects', str(self.projects)]
        with patch('orchd.home_migrations.ensure_current') as migrate, patch.object(store, 'connect') as database:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(args), 0)
            migrate.assert_not_called()
            database.assert_not_called()
        with patch.dict(os.environ, {'ORCHD_WORKTREE': str(self.folder / 'task')}):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(args), 1)
        self.assertFalse(self.root.exists())


if __name__ == '__main__':
    unittest.main()
