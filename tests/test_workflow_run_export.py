"""Offline export checks use temporary Git worktrees and synthetic run records."""
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from orchd import store, workflow_run_export as export, workflows


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.worktree = self.root / 'example-service'
        self.worktree.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.worktree)], check=True)
        (self.worktree / '.gitignore').write_text('.local/\n')
        (self.worktree / '.local').mkdir()
        self.con = store.connect(self.root / 'fixture.db')
        self.addCleanup(self.con.close)
        pin = dict(name='example-workflow', commit='a' * 40, digest='sha256:' + 'b' * 64)
        self.plan = dict(schema_version='orchd-run-plan/v1', workflow=pin,
                         context={'request': 'Exact context\n'}, steps=[
            dict(id='prepare', repo='example-hub', stage='prepare', inputs={'request': {'context': 'request'}}, outputs=['instructions']),
            dict(id='implement', repo='example-service', stage='implement',
                 inputs={'instructions': {'output': {'step': 'prepare', 'field': 'instructions'}}}, outputs=['head'],
                 verification=dict(repo='example-service', stage='review', inputs={}, outputs=[])),
            dict(id='audit', repo='example-config', stage='audit',
                 inputs={'author_task': {'task': {'step': 'implement', 'field': 'task_id'}}}, outputs=[])
        ])
        self.con.execute('INSERT INTO workflow_runs(id,request_id,orch_thread,idempotency_key,plan,plan_digest,origin,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                         ('run-one', 1, 'example-orch', 'request-one', workflows.canonical(self.plan), 'sha256:' + 'c' * 64, '{}', 'active', 1, 1))
        self.task('prepare1', 'example-hub', 0, output={'instructions': 'Original instructions\n'})
        self.task('author1', 'example-service', 1)
        self.task('review1', 'example-service', 1, role='reviewer')
        self.task('audit1', 'example-config', 2)
        self.message('prepare1', 'dispatch', json.dumps(dict(workflow=dict(pin, stage='prepare', inputs={'request': 'Exact context\n'}))))
        self.message('prepare1', 'report', 'done: original self-report')
        self.message('author1', 'dispatch', json.dumps(dict(workflow=dict(pin, stage='implement', inputs={'instructions': 'Original instructions\n'}))))
        self.message('review1', 'dispatch', json.dumps(dict(workflow=dict(pin, stage='review', inputs={}))))
        self.message('audit1', 'ack', 'starting audit')
        self.con.commit()

    def task(self, task_id, repo, index=None, role='author', output=None):
        self.con.execute('INSERT INTO tasks(id,repo,repo_path,title,instructions,done_when,orch_thread,codex_bin,status,created_at,updated_at,worktree) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                         (task_id, repo, str(self.worktree), 'fixture', 'private instructions', 'fixture', 'example-orch', 'fixture', 'running', 1, 1, str(self.worktree)))
        if index is not None:
            raw = workflows.canonical(dict(schema_version='orchd-workflow-output/v1', values=output)) if output is not None else None
            self.con.execute('INSERT INTO workflow_run_attempts(id,run_id,step_index,role,task_id,state,spec,output) VALUES(?,?,?,?,?,?,?,?)',
                             ('attempt-' + task_id, 'run-one', index, role, task_id, 'running', b'{}', raw))

    def message(self, task_id, kind, body):
        self.con.execute('INSERT INTO messages(task_id,kind,body,evidence,created_at) VALUES(?,?,?,?,?)',
                         (task_id, kind, body, 'original evidence', 123.5))

    def run_export(self, target='.local/export', env=None):
        return export.export_for_task(self.con, 'audit1', target, env={} if env is None else env)

    def test_exact_scoped_messages_real_mapping_and_hash_receipt(self):
        self.task('other-task', 'other-service')
        self.message('other-task', 'report', 'SECRET_OTHER_RUN')
        self.message('audit1', 'answer', 'SECRET_UNSUPPORTED_CHAT')
        result = self.run_export()
        root = Path(result['output_dir'])
        plan = json.loads((root / 'auditplan.json').read_bytes())
        self.assertEqual([s['task_ids'] for s in plan['stages']], [['prepare1'], ['author1'], ['review1'], ['audit1']])
        self.assertEqual(plan['stages'][2]['workflow_stage'], 'review')
        self.assertEqual(plan['stages'][2]['repository'], 'example-service')
        expected = export._digest(b'Original instructions\n')
        self.assertEqual(plan['stages'][1]['input_digests'], {'instructions': expected})
        self.assertEqual(plan['stages'][3]['input_digests'], {'author_task': export._digest(b'author1')})
        records = [json.loads(line) for line in (root / 'core.jsonl').read_bytes().splitlines()]
        self.assertEqual(len(records), 5)
        self.assertEqual(records[1]['body'], 'done: original self-report')
        self.assertEqual(records[1]['evidence'], 'original evidence')
        self.assertEqual(records[1]['created_at'], 123.5)
        self.assertEqual(records[1]['repo'], 'example-hub')
        self.assertFalse(any(r['task_id'] == 'audit1' and r['kind'] == 'report' for r in records))
        self.assertNotIn(b'SECRET', (root / 'core.jsonl').read_bytes())
        for name, digest in result['files'].items():
            self.assertEqual(digest, 'sha256:' + hashlib.sha256((root / name).read_bytes()).hexdigest())
        state = json.loads((root / 'run.json').read_bytes())
        self.assertEqual(state['state'], 'active')
        first = next(a for a in state['attempts'] if a['task_id'] == 'prepare1')
        self.assertEqual(first['output_values'], {'instructions': 'Original instructions\n'})
        self.assertEqual(first['worktree'], str(self.worktree))
        raw = self.con.execute("SELECT output FROM workflow_run_attempts WHERE task_id='prepare1'").fetchone()[0]
        self.assertEqual(first['output_raw_digest'], export._digest(raw))
        self.assertNotIn('private instructions', json.dumps(state))
        self.assertEqual(json.loads((root / 'receipt.json').read_bytes()), result)

    def test_deferred_closes_are_not_exported_as_false_close_dependencies(self):
        for task in ('author1', 'review1', 'audit1'):
            self.message(task, 'report', 'done: actual worker self-report')
        # All closes happen after downstream dispatch, as required by producer retention.
        for task in ('prepare1', 'author1', 'review1', 'audit1'):
            self.con.execute('INSERT INTO messages(task_id,kind,body,created_at) VALUES(?,?,?,?)',
                             (task, 'close', '{"outcome":"done"}', 999))
        root = Path(self.run_export()['output_dir'])
        plan = json.loads((root / 'auditplan.json').read_bytes())
        self.assertTrue(all('depends_on' not in stage for stage in plan['stages']))
        state = json.loads((root / 'run.json').read_bytes())
        self.assertEqual(state['frozen_plan'], self.plan)
        self.assertEqual([edge['gate'] for edge in state['expected_transitions']],
                         ['task_report', 'author_report', 'report_and_native_verification'])
        self.assertTrue(all(edge['audit_support'] == 'not_checked_by_close_based_auditor'
                            for edge in state['expected_transitions']))
        # The export retains late real closes rather than inventing earlier completion events.
        rows = [json.loads(line) for line in (root / 'core.jsonl').read_bytes().splitlines()]
        self.assertEqual([row['created_at'] for row in rows if row['kind'] == 'close'], [999.0] * 4)

    def test_worker_identity_and_output_scope_are_checked_before_writes(self):
        for env in ({'ORCHD_TASK_ID': 'author1'}, {'ORCHD_WORKTREE': str(self.root)}):
            with self.subTest(env=env), self.assertRaisesRegex(ValueError, 'identity'):
                self.run_export(env=env)
        for path in (str(self.root / 'outside'), '../outside', '.local/../bad', '.git/export'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.run_export(path)
        self.assertFalse((self.worktree / '.local/export').exists())
        self.assertFalse((self.root / 'outside').exists())

    def test_unmapped_task_is_refused_without_creating_directory(self):
        self.task('standalone', 'example-service')
        with self.assertRaisesRegex(ValueError, 'exactly one'):
            export.export_for_task(self.con, 'standalone', '.local/export', env={})
        self.assertFalse((self.worktree / '.local/export').exists())

    def test_missing_attempt_is_unassigned_not_a_synthetic_task(self):
        self.con.execute("DELETE FROM workflow_run_attempts WHERE task_id='review1'")
        root = Path(self.run_export()['output_dir'])
        plan = json.loads((root / 'auditplan.json').read_bytes())
        self.assertEqual(plan['stages'][2]['task_ids'], [])
        self.assertNotIn('review1', (root / 'core.jsonl').read_text())

    def test_reserved_task_without_spawn_row_keeps_its_identity(self):
        self.con.execute("DELETE FROM messages WHERE task_id='review1'")
        self.con.execute("DELETE FROM tasks WHERE id='review1'")
        root = Path(self.run_export()['output_dir'])
        plan = json.loads((root / 'auditplan.json').read_bytes())
        self.assertEqual(plan['stages'][2]['task_ids'], ['review1'])
        state = json.loads((root / 'run.json').read_bytes())
        pending = next(a for a in state['attempts'] if a['task_id'] == 'review1')
        self.assertIsNone(pending['repo'])
        self.assertIsNone(pending['task_status'])
        self.assertIsNone(pending['worktree'])

    def test_unavailable_binding_remains_explicit(self):
        self.con.execute("UPDATE workflow_run_attempts SET output=NULL WHERE task_id='prepare1'")
        root = Path(self.run_export()['output_dir'])
        state = json.loads((root / 'run.json').read_bytes())
        self.assertEqual(state['unresolved_inputs'], [dict(stage_id='step-001-author', input='instructions', reason='binding_not_available_at_export')])
        self.assertNotIn('input_digests', json.loads((root / 'auditplan.json').read_bytes())['stages'][1])

    def test_existing_or_nonignored_outputs_are_preserved(self):
        root = self.worktree / '.local/export'
        root.mkdir()
        (root / 'original').write_text('preserve')
        with self.assertRaisesRegex(ValueError, 'new directory'):
            self.run_export()
        self.assertEqual((root / 'original').read_text(), 'preserve')
        with self.assertRaisesRegex(ValueError, 'ignored'):
            self.run_export('untracked-export')
        self.assertFalse((self.worktree / 'untracked-export').exists())

    def test_read_snapshot_does_not_commit_an_existing_caller_transaction(self):
        self.con.execute('BEGIN')
        self.message('audit1', 'report', 'blocked: local uncommitted fixture')
        self.assertTrue(self.con.in_transaction)
        self.run_export()
        self.assertTrue(self.con.in_transaction)
        self.con.rollback()
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM messages WHERE body='blocked: local uncommitted fixture'").fetchone()[0], 0)

    def test_symlink_parent_is_refused(self):
        (self.worktree / '.local/link').symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            self.run_export('.local/link/export')
        self.assertFalse((self.root / 'export').exists())

    def test_no_messages_are_inserted_and_caller_transaction_is_not_committed(self):
        count = self.con.execute('SELECT COUNT(*) FROM messages').fetchone()[0]
        changes = self.con.total_changes
        self.run_export()
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM messages').fetchone()[0], count)
        self.assertEqual(self.con.total_changes, changes)
        self.assertFalse(self.con.in_transaction)


if __name__ == '__main__':
    unittest.main()
