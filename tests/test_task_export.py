"""Exercise real single-stage records, missing evidence and a deliberately dropped requirement."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from orchd import cli, store, task_export, workflows


class TaskExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.worktree = self.root / 'example-service'
        self.worktree.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.worktree)], check=True)
        (self.worktree / '.gitignore').write_text('.local/\n')
        (self.worktree / '.local').mkdir()
        self.con = store.connect(self.root / 'state.db')
        self.addCleanup(self.con.close)
        self.request = 'AC1: implement session events.\nAC2: also clear analytics on logout.\n'
        self.con.execute('INSERT INTO tasks(id,repo,repo_path,title,instructions,done_when,orch_thread,'
                         'codex_bin,status,created_at,updated_at,worktree) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                         ('task-one', 'example-service', str(self.worktree), 'fixture',
                          'Workflow boilerplate\n' + self.request, 'Both requirements', 'orch-one',
                          'fixture', 'done', 1, 2, str(self.worktree)))
        self.con.execute('INSERT INTO interface_requests(id,interface_id,orch_id,conversation,request_key,body,created_at) '
                         'VALUES(1,?,?,?,?,?,?)', ('adapter-one', 'orch-one', 'thread-one', 'event-one', self.request, 1))
        self.dispatch(self.request)
        self.con.execute("INSERT INTO messages(task_id,kind,body,created_at) VALUES('other-task','report','UNRELATED_SECRET',2)")

    def dispatch(self, text, extra=None):
        self.con.execute("DELETE FROM messages WHERE task_id='task-one' AND kind='dispatch'")
        self.con.execute('INSERT INTO messages(task_id,kind,body,created_at) VALUES(?,?,?,?)',
                         ('task-one', 'dispatch', json.dumps(dict(workflow=dict(
                             name='example-flow', stage='investigate', commit='a' * 40,
                             digest='sha256:' + 'b' * 64, inputs=dict(question=text, **(extra or {}))))), 1))

    def export(self, name='evidence', **kw):
        return task_export.export_task(self.con, 'task-one', '.local/' + name,
                                       env=kw.pop('env', {}), **kw)

    def test_standalone_export_preserves_original_dispatch_and_hashes(self):
        result = self.export(request_id=1)
        root = Path(result['output_dir'])
        self.assertEqual(result['comparison']['status'], 'identical')
        self.assertTrue(result['comparison']['request_in_worker_instructions'])
        self.assertEqual((root / 'request.txt').read_bytes(), self.request.encode())
        self.assertEqual((root / 'dispatch-input.txt').read_bytes(), self.request.encode())
        self.assertEqual((root / 'request-to-dispatch.diff').read_bytes(), b'')
        task = json.loads((root / 'task.json').read_bytes())
        self.assertEqual(task['instructions'], 'Workflow boilerplate\n' + self.request)
        self.assertEqual(json.loads((root / 'workflow.json').read_bytes())['commit'], 'a' * 40)
        self.assertEqual(result['request_selection'], 'explicit_same_orch_not_origin_attestation')
        self.assertNotIn(b'UNRELATED_SECRET', (root / 'core.jsonl').read_bytes())
        self.assertEqual(result, json.loads((root / 'receipt.json').read_bytes()))
        for name, digest in result['files'].items():
            self.assertEqual(digest, workflows.digest((root / name).read_bytes()))
        self.assertEqual((root.stat().st_mode & 0o777), 0o700)
        self.assertEqual(((root / 'request.txt').stat().st_mode & 0o777), 0o600)

    def test_dropped_original_requirement_is_visible_in_diff(self):
        narrowed = 'AC2: also clear analytics on logout.\n'
        self.dispatch(narrowed)
        result = self.export(request_id=1)
        self.assertEqual(result['comparison']['status'], 'different')
        self.assertIn('-AC1: implement session events.',
                      (Path(result['output_dir']) / 'request-to-dispatch.diff').read_text())
        self.assertEqual(self.con.execute('SELECT body FROM interface_requests WHERE id=1').fetchone()[0], self.request)

    def test_missing_request_input_or_dispatch_is_never_identical(self):
        self.assertEqual(self.export('no-request')['comparison']['status'], 'missing')
        self.dispatch(self.request, {'other': 'unrelated input'})
        self.assertEqual(self.export('ambiguous', request_id=1)['comparison']['status'], 'missing')
        self.assertEqual(self.export('selected', request_id=1, input_key='question')['comparison']['status'], 'identical')
        self.assertEqual(self.export('unknown-key', request_id=1, input_key='absent')['comparison']['status'], 'missing')
        self.con.execute("DELETE FROM messages WHERE task_id='task-one'")
        self.assertEqual(self.export('no-dispatch', request_id=1)['comparison']['status'], 'missing')

    def test_trailing_newline_is_not_silently_normalized(self):
        self.dispatch(self.request.rstrip('\n'))
        result = self.export(request_id=1)
        self.assertEqual(result['comparison']['status'], 'different')
        self.assertNotEqual(result['comparison']['request_digest'], result['comparison']['dispatch_input_digest'])

    def test_workflow_input_equality_does_not_hide_changed_worker_instructions(self):
        self.con.execute("UPDATE tasks SET instructions='A narrowed worker instruction' WHERE id='task-one'")
        result = self.export(request_id=1)
        self.assertEqual(result['comparison']['status'], 'identical')
        self.assertFalse(result['comparison']['request_in_worker_instructions'])

    def test_request_scope_and_worker_identity_refuse_before_writing(self):
        for env in ({'ORCHD_TASK_ID': 'another-task'}, {'ORCHD_ORCH_ID': 'another-orch'},
                    {'ORCHD_WORKTREE': str(self.root)}):
            with self.subTest(env=env), self.assertRaises(ValueError):
                self.export(request_id=1, env=env)
        self.con.execute("UPDATE interface_requests SET orch_id='another-orch' WHERE id=1")
        with self.assertRaisesRegex(ValueError, 'different Orchs'):
            self.export(request_id=1)
        self.assertFalse((self.worktree / '.local/evidence').exists())

    def test_unknown_request_refuses_and_unknown_task_does_not_scan(self):
        with self.assertRaisesRegex(ValueError, 'request is unavailable'):
            self.export(request_id=99)
        with self.assertRaisesRegex(ValueError, 'Task is unavailable'):
            task_export.export_task(self.con, 'absent', self.worktree / '.local/evidence', request_id=1, env={})
        self.assertFalse((self.worktree / '.local/evidence').exists())

    def test_existing_nonignored_or_symlink_outputs_are_preserved(self):
        self.export(request_id=1)
        with self.assertRaisesRegex(ValueError, 'new directory'):
            self.export(request_id=1)
        with self.assertRaisesRegex(ValueError, 'ignored'):
            task_export.export_task(self.con, 'task-one', self.worktree / 'visible', request_id=1, env={})
        (self.worktree / '.local/link').symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            self.export('link/out', request_id=1)

    def test_export_does_not_commit_callers_transaction(self):
        self.con.execute('BEGIN')
        self.con.execute("UPDATE tasks SET title='uncommitted' WHERE id='task-one'")
        self.export(request_id=1)
        self.assertTrue(self.con.in_transaction)
        self.con.rollback()
        self.assertEqual(self.con.execute("SELECT title FROM tasks WHERE id='task-one'").fetchone()[0], 'fixture')

    def test_cli_readonly_snapshot_and_check_exit_codes(self):
        for index, (text, request, expected) in enumerate([
            (self.request, '1', 0), ('Only a narrower task\n', '1', 1), (self.request, None, 2)
        ]):
            self.dispatch(text)
            argv = ['task', 'export', '--task-id', 'task-one',
                    '--output', str(self.worktree / '.local' / str(index)), '--check'] \
                   + (['--request-id', request] if request else [])
            snapshot = store.connect_readonly(self.root / 'state.db')
            with patch.dict('os.environ', {}, clear=True), \
                    patch.object(store, 'connect_readonly', return_value=snapshot), \
                    patch('orchd.home_migrations.ensure_current') as migrate, \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(argv), expected)
                migrate.assert_not_called()
