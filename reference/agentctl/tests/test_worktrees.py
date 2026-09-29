import concurrent.futures
import importlib.util
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import sys
import threading
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location('worktrees', Path(__file__).resolve().parents[1] / 'bin/worktrees.py')
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)


class WorktreesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.git('init', '-b', 'trunk')
        self.git('config', 'user.name', 'Test')
        self.git('config', 'user.email', 'test@example.invalid')
        (self.repo / 'a').write_text('base\n')
        self.git('add', 'a')
        self.git('commit', '-m', 'base')
        self.base = self.git('rev-parse', 'HEAD')
        self.dbfile = self.root / 'state.sqlite'
        self.con = self.connect()
        self.addCleanup(self.con.close)
        self.con.execute('CREATE TABLE tasks(id TEXT PRIMARY KEY)')
        self.con.executemany('INSERT INTO tasks VALUES(?)', [('one',), ('two',)])
        self.con.commit()
        w.schema(self.con)

    def connect(self):
        con = sqlite3.connect(self.dbfile, timeout=10)
        con.row_factory = sqlite3.Row
        return con

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], stderr=subprocess.DEVNULL, text=True).strip()

    def create(self, **kwargs):
        values = dict(task_id='one', repo=str(self.repo), base='trunk', path=str(self.root / 'work'), branch='task-one')
        values.update(kwargs)
        return w.create(self.con, **values)

    def test_dirty_source_preserved_and_checkpoint(self):
        (self.repo / 'a').write_text('source dirty\n')
        (self.repo / 'private').write_text('source untracked')
        row = self.create()
        self.assertEqual(row['state'], 'ready')
        self.assertEqual((self.repo / 'a').read_text(), 'source dirty\n')
        target = Path(row['path'])
        self.assertEqual((target / 'a').read_text(), 'base\n')
        (target / 'a').write_text('SECRET\n')
        (target / 'new').write_text('UNTRACKED_SECRET')
        summary = w.checkpoint(self.con, row['id'])
        self.assertNotIn('SECRET', str(summary))
        self.assertTrue(summary['has_diff'])
        self.assertEqual(summary['untracked_count'], 1)
        saved = self.con.execute('SELECT * FROM worktree_checkpoints').fetchone()
        self.assertIn('SECRET', saved['diff'])
        self.assertEqual(saved['untracked'], '["new"]')

    def test_base_is_pinned_even_when_ref_moves(self):
        original = w.git
        def moving(repo, *args):
            if args[:2] == ('worktree', 'add'):
                (self.repo / 'a').write_text('new trunk\n')
                self.git('commit', '-am', 'advance')
            return original(repo, *args)
        with mock.patch.object(w, 'git', side_effect=moving):
            row = self.create()
        self.assertEqual(row['base_revision'], self.base)
        self.assertEqual(w.git(row['path'], 'rev-parse', 'HEAD'), self.base)
        self.assertNotEqual(self.git('rev-parse', 'HEAD'), self.base)

    def test_reject_existing_paths_branches_and_non_root(self):
        target = self.root / 'work'
        target.mkdir()
        with self.assertRaisesRegex(RuntimeError, 'already exists'):
            self.create()
        target.rmdir()
        self.git('branch', 'task-one')
        with self.assertRaisesRegex(RuntimeError, 'branch already exists'):
            self.create()
        nested = self.repo / 'nested'
        nested.mkdir()
        with self.assertRaisesRegex(RuntimeError, 'exact Git'):
            self.create(repo=str(nested))
        self.assertEqual(self.con.execute('SELECT count(*) FROM managed_worktrees').fetchone()[0], 0)

    def test_parallel_task_repo_reservation(self):
        def attempt(i):
            con = self.connect()
            try:
                return w.create(con, 'one', str(self.repo), 'trunk', str(self.root / ('work%d' % i)), 'branch%d' % i)['state']
            except RuntimeError:
                return 'rejected'
            finally:
                con.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, range(2)))
        self.assertCountEqual(results, ['ready', 'rejected'])
        self.assertEqual(self.con.execute('SELECT count(*) FROM managed_worktrees').fetchone()[0], 1)

    def test_failure_retains_reservation(self):
        original = w.git
        def failing(repo, *args):
            if args[:2] == ('worktree', 'add'):
                raise RuntimeError('simulated git failure')
            return original(repo, *args)
        with mock.patch.object(w, 'git', side_effect=failing):
            with self.assertRaisesRegex(RuntimeError, 'reservation retained'):
                self.create()
        row = self.con.execute('SELECT * FROM managed_worktrees').fetchone()
        self.assertEqual(row['state'], 'needs_inspection')
        with self.assertRaisesRegex(RuntimeError, 'already reserved'):
            self.create()

    def test_indexed_source_syncs_without_initializing(self):
        (self.repo / '.codegraph').mkdir()
        original = subprocess.run
        syncs = []
        def runner(command, **kwargs):
            if command[0].endswith('/codegraph'):
                syncs.append((command, kwargs['cwd']))
                return subprocess.CompletedProcess(command, 0)
            return original(command, **kwargs)
        with mock.patch.object(w.subprocess, 'run', side_effect=runner):
            row = self.create()
        self.assertEqual(len(syncs), 1)
        self.assertEqual(syncs[0][0][1:], ['sync'])
        self.assertEqual(syncs[0][1], row['path'])

    def test_interrupted_reservation_is_not_reused(self):
        row = self.create()
        self.con.execute("UPDATE managed_worktrees SET state='creating' WHERE id=?", (row['id'],))
        self.con.commit()
        with self.assertRaisesRegex(RuntimeError, 'already reserved'):
            self.create(path=str(self.root / 'new-path'), branch='new-branch')
        with self.assertRaisesRegex(RuntimeError, 'not ready'):
            w.checkpoint(self.con, row['id'])

    def test_linked_source_cannot_bypass_task_repo_uniqueness(self):
        row = self.create()
        with self.assertRaisesRegex(RuntimeError, 'already reserved'):
            self.create(repo=row['path'], path=str(self.root / 'alias'), branch='alias')

    def test_git_environment_cannot_redirect_repository(self):
        with mock.patch.dict(os.environ, {'GIT_DIR': '/does/not/exist', 'GIT_WORK_TREE': '/wrong'}):
            row = self.create(integration=True)
            self.assertEqual(row['base_revision'], self.base)
            result = w.integrate(self.con, row['id'], [self.base])
            self.assertEqual(result['state'], 'merged')
            checked = w.verify(self.con, row['id'], [sys.executable, '-c',
                                "import os; assert 'GIT_DIR' not in os.environ"])
            self.assertEqual(checked['state'], 'passed')

    def test_registered_project_checks_common_git_identity(self):
        self.con.executescript("ALTER TABLE tasks ADD COLUMN project TEXT; CREATE TABLE projects(id TEXT); CREATE TABLE project_repos(project_id TEXT,root TEXT,identity TEXT);")
        self.con.execute("UPDATE tasks SET project='project' WHERE id='one'")
        self.con.execute("INSERT INTO projects VALUES('project')")
        self.con.execute("INSERT INTO project_repos VALUES('project','/elsewhere','/elsewhere/.git')")
        self.con.commit()
        with self.assertRaisesRegex(RuntimeError, 'registered task project'):
            self.create()
        self.con.execute('UPDATE project_repos SET identity=?', (str(self.repo / '.git'),))
        self.con.commit()
        self.assertEqual(self.create()['state'], 'ready')

    def feature(self, branch, content, filename='a'):
        path = self.root / branch
        self.git('worktree', 'add', '-b', branch, str(path), self.base)
        (path / filename).write_text(content)
        w.git(path, 'add', filename)
        w.git(path, 'commit', '-m', branch)
        return w.git(path, 'rev-parse', 'HEAD')

    def test_integration_pins_inputs_and_leaves_target_unchanged(self):
        first = self.feature('feature-one', 'one', 'one')
        second = self.feature('feature-two', 'two', 'two')
        row = self.create(integration=True)
        result = w.integrate(self.con, row['id'], [first, second])
        self.assertEqual(result['state'], 'merged')
        self.assertEqual(self.git('rev-parse', 'trunk'), self.base)
        self.assertEqual(self.git('symbolic-ref', '--short', 'HEAD'), 'trunk')
        for revision in [first, second]:
            w.git(row['path'], 'merge-base', '--is-ancestor', revision, 'HEAD')
        operation = self.con.execute('SELECT * FROM worktree_operations').fetchone()
        self.assertIn(first, operation['inputs'])
        self.assertEqual(operation['result_revision'], result['revision'])
        passed = w.verify(self.con, row['id'], [sys.executable, '-c', 'pass'])
        self.assertEqual(passed['state'], 'passed')
        failed = w.verify(self.con, row['id'], [sys.executable, '-c', 'raise SystemExit(7)'])
        self.assertEqual(failed['state'], 'failed')
        self.assertEqual(failed['exit_code'], 7)

    def test_integration_conflicts_preserve_claim_and_files(self):
        first = self.feature('feature-one', 'one\n')
        second = self.feature('feature-two', 'two\n')
        row = self.create(integration=True)
        with self.assertRaisesRegex(RuntimeError, 'needs_inspection'):
            w.integrate(self.con, row['id'], [first, second])
        self.assertIn('<<<<<<<', (Path(row['path']) / 'a').read_text())
        self.assertEqual(self.git('rev-parse', 'trunk'), self.base)
        self.assertEqual(self.con.execute('SELECT count(*) FROM resource_claims').fetchone()[0], 1)
        with self.assertRaisesRegex(RuntimeError, 'active writer'):
            w.verify(self.con, row['id'], [sys.executable, '-c', 'pass'])

    def test_conflict_resolution_recovery_preserves_target(self):
        first = self.feature('feature-one', 'one\n')
        second = self.feature('feature-two', 'two\n')
        row = self.create(integration=True)
        with self.assertRaises(RuntimeError):
            w.integrate(self.con, row['id'], [first, second])
        oid = self.con.execute('SELECT id FROM worktree_operations').fetchone()[0]
        with self.assertRaisesRegex(RuntimeError, 'clean working tree'):
            w.recover(self.con, oid, 'writers stopped')
        (Path(row['path']) / 'a').write_text('resolved\n')
        w.git(row['path'], 'add', 'a')
        w.git(row['path'], 'commit', '-m', 'resolve')
        result = w.recover(self.con, oid, 'Git process finished; checked descendant writers stopped')
        self.assertEqual(result['state'], 'recovered')
        self.assertEqual(self.git('rev-parse', 'trunk'), self.base)
        self.assertEqual(self.con.execute('SELECT count(*) FROM resource_claims').fetchone()[0], 0)
        self.assertEqual(w.verify(self.con, row['id'], [sys.executable, '-c', 'pass'])['state'], 'passed')

    def test_recovery_rejects_live_owner_wrong_claim_and_active_dispatch(self):
        row = self.create(integration=True)
        oid, _ = w.reserve_operation(self.con, row['id'], 'integrate', [self.base])
        with self.assertRaisesRegex(RuntimeError, 'still running'):
            w.recover(self.con, oid, 'assertion cannot override live owner')
        self.con.execute("UPDATE worktree_operations SET state='needs_inspection' WHERE id=?", (oid,))
        self.con.execute("UPDATE resource_claims SET attempt_id='other'")
        self.con.commit()
        with self.assertRaisesRegex(RuntimeError, 'does not belong'):
            w.recover(self.con, oid, 'stopped')
        self.con.execute('UPDATE resource_claims SET attempt_id=?', (oid,))
        self.con.executescript('CREATE TABLE workers(id TEXT,cwd TEXT); CREATE TABLE messages(recipient TEXT,kind TEXT,state TEXT);')
        self.con.execute('INSERT INTO workers VALUES(?,?)', ('worker', row['path']))
        self.con.execute("INSERT INTO messages VALUES('worker','dispatch','sent')")
        self.con.commit()
        with self.assertRaisesRegex(RuntimeError, 'unresolved worker dispatch'):
            w.recover(self.con, oid, 'stopped')

    def test_recovery_rejects_unfinished_merge_even_with_clean_index(self):
        row = self.create(integration=True)
        oid, _ = w.reserve_operation(self.con, row['id'], 'integrate', [self.base])
        self.con.execute("UPDATE worktree_operations SET state='needs_inspection' WHERE id=?", (oid,))
        self.con.commit()
        merge_head = Path(w.git(row['path'], 'rev-parse', '--git-path', 'MERGE_HEAD'))
        merge_head.write_text(self.base + '\n')
        with self.assertRaisesRegex(RuntimeError, 'existing Git operation'):
            w.recover(self.con, oid, 'stopped')
        self.assertEqual(self.con.execute('SELECT count(*) FROM resource_claims').fetchone()[0], 1)

    def test_integration_rejects_feature_tree_and_dirty_tree(self):
        row = self.create()
        with self.assertRaisesRegex(RuntimeError, 'dedicated'):
            w.integrate(self.con, row['id'], [self.base])
        row = self.create(task_id='two', path=str(self.root / 'integration'), branch='integration', integration=True)
        (Path(row['path']) / 'untracked').write_text('preserve')
        with self.assertRaisesRegex(RuntimeError, 'rejected'):
            w.integrate(self.con, row['id'], [self.base])
        self.assertEqual(self.con.execute('SELECT count(*) FROM resource_claims').fetchone()[0], 0)
        with self.assertRaisesRegex(RuntimeError, 'full commit'):
            w.integrate(self.con, row['id'], ['trunk'])

    def test_parallel_integration_reserves_writer_before_git_io(self):
        row = self.create(integration=True)
        entered, proceed = threading.Event(), threading.Event()
        original = w.git
        def gated(repo, *args):
            if 'merge' in args:
                entered.set()
                if not proceed.wait(5):
                    raise RuntimeError('test timed out')
            return original(repo, *args)
        def attempt():
            con = self.connect()
            try:
                return w.integrate(con, row['id'], [self.base])
            finally:
                con.close()
        with mock.patch.object(w, 'git', side_effect=gated):
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(attempt)
                try:
                    self.assertTrue(entered.wait(5))
                    with self.assertRaisesRegex(RuntimeError, 'active writer'):
                        w.integrate(self.con, row['id'], [self.base])
                finally:
                    proceed.set()
                self.assertEqual(future.result()['state'], 'merged')

    def test_two_tasks_have_independent_branches(self):
        first = self.create()
        second = self.create(task_id='two', path=str(self.root / 'other'), branch='task-two')
        (Path(first['path']) / 'a').write_text('one dirty\n')
        self.assertEqual((Path(second['path']) / 'a').read_text(), 'base\n')
        self.assertEqual(self.git('symbolic-ref', '--short', 'HEAD'), 'trunk')


if __name__ == '__main__':
    unittest.main()
