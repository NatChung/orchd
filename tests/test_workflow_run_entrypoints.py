import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from orchd import cli, mcp_server, workflow_run_cli, workflow_runs


class WorkflowRunEntrypoints(unittest.TestCase):
    def test_start_preserves_input_bytes_and_arms_the_durable_runner(self):
        plan = {'context': {'instructions': '原文\n\n'}}
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder) / 'plan.json'
            file.write_text(json.dumps(plan))
            args = cli.build_parser().parse_args([
                'workflow', 'run', 'start', '--plan-file', str(file),
                '--request-id', '7', '--idempotency-key', 'request-7', '--orch-id', 'owner'])
            con, rt = mock.Mock(), mock.Mock()
            with mock.patch.dict(os.environ, {}, clear=True), \
                 mock.patch.object(workflow_run_cli.store, 'connect', return_value=con), \
                 mock.patch.object(workflow_run_cli, 'Runtime', return_value=rt), \
                 mock.patch.object(workflow_runs, 'start', return_value={'run_id': 'run'}) as start, \
                 mock.patch.object(workflow_runs, 'ensure_runner') as runner, \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(workflow_run_cli.run(args, lambda db, name: name), 0)
            self.assertEqual(start.call_args.kwargs['plan']['context']['instructions'], '原文\n\n')
            self.assertEqual(start.call_args.kwargs['request_id'], 7)
            self.assertEqual(start.call_args.kwargs['orch_thread'], 'owner')
            runner.assert_called_once_with(con, rt)
            con.close.assert_called_once()

    def test_duplicate_plan_keys_refuse_before_dispatch(self):
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder) / 'plan.json'
            file.write_text('{"context":{},"context":{"instructions":"changed"}}')
            args = SimpleNamespace(run_action='start', plan_file=file, request_id=1,
                                   idempotency_key='key', orch_id='owner')
            with mock.patch.dict(os.environ, {}, clear=True), \
                 mock.patch.object(workflow_run_cli.store, 'connect'), \
                 mock.patch.object(workflow_run_cli, 'Runtime'), \
                 mock.patch.object(workflow_runs, 'start') as start, \
                 contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(workflow_run_cli.run(args, lambda db, name: name), 1)
            start.assert_not_called()

    def test_orch_uses_installed_graph_and_cannot_replace_it_with_a_plan_file(self):
        args = cli.build_parser().parse_args(['workflow', 'run', 'start', '--workflow', 'example',
            '--template', 'delivery', '--context', '{"request":"original\\n"}',
            '--request-id', '7', '--idempotency-key', 'once', '--orch-id', 'owner'])
        fixed = {'context': {'request': 'original\n'}, 'steps': ['installed graph']}
        with mock.patch.dict(os.environ, {'ORCHD_ORCH_ID': 'owner'}, clear=True), \
             mock.patch.object(workflow_run_cli.store, 'connect'), \
             mock.patch.object(workflow_run_cli, 'Runtime'), \
             mock.patch.object(workflow_run_cli.workflows, 'run_template', return_value=fixed) as template, \
             mock.patch.object(workflow_runs, 'start', return_value={'run_id': 'r'}) as start, \
             mock.patch.object(workflow_runs, 'ensure_runner'), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(workflow_run_cli.run(args, lambda db, owner: owner), 0)
        template.assert_called_once_with('example', 'delivery', {'request': 'original\n'})
        self.assertEqual(start.call_args.kwargs['plan'], fixed)
        args = SimpleNamespace(run_action='start', plan_file=Path('arbitrary.json'), orch_id='owner')
        with mock.patch.dict(os.environ, {'ORCHD_ORCH_ID': 'owner'}, clear=True), \
             mock.patch.object(workflow_run_cli.store, 'connect'), \
             mock.patch.object(workflow_run_cli, 'Runtime'), \
             mock.patch.object(workflow_runs, 'start') as start, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(workflow_run_cli.run(args, lambda db, owner: owner), 1)
        start.assert_not_called()

    def test_worker_cannot_start_service_or_choose_a_new_origin(self):
        args = SimpleNamespace(run_action='serve', database=Path('other.db'), interval=1, once=True)
        with mock.patch.dict(os.environ, {'ORCHD_WORKTREE': '/worker'}), \
             mock.patch.object(workflow_runs, 'serve') as serve, \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(workflow_run_cli.run(args, None), 1)
        serve.assert_not_called()
        with mock.patch.dict(os.environ, {'ORCHD_WORKTREE': '/worker'}), \
             mock.patch.object(workflow_runs, 'start') as start:
            with self.assertRaisesRegex(ValueError, 'Workers'):
                mcp_server.call('workflow_run_start', {'workflow': 'example', 'template': 'delivery', 'context': {}, 'request_id': 1,
                                'idempotency_key': 'x'}, 'owner', None, None)
        start.assert_not_called()

    def test_origin_override_is_rejected_at_mcp_boundary(self):
        with self.assertRaisesRegex(ValueError, 'Unexpected'):
            mcp_server.call('workflow_run_start', {'workflow': 'example', 'template': 'delivery', 'context': {}, 'request_id': 1,
                'idempotency_key': 'x', 'conversation': 'different-chat'}, 'owner', None, None)

    def test_cancel_requires_an_owned_control_call_and_keeps_pending_cleanup_running(self):
        con, rt = object(), object()
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(workflow_runs, 'cancel', return_value={'state': 'cancelling'}) as cancel, \
             mock.patch.object(workflow_runs, 'ensure_runner') as runner:
            result = mcp_server.call('workflow_run_cancel', {'run_id': 'r', 'reason': 'test stopped'},
                                     'actual-owner', con, rt)
        self.assertEqual(result['state'], 'cancelling')
        cancel.assert_called_once_with(con, rt, 'r', orch_thread='actual-owner', reason='test stopped')
        runner.assert_called_once_with(con, rt)
        with mock.patch.dict(os.environ, {'ORCHD_WORKTREE': '/worker'}), \
             mock.patch.object(workflow_runs, 'cancel') as cancel:
            with self.assertRaisesRegex(ValueError, 'Workers'):
                mcp_server.call('workflow_run_cancel', {'run_id': 'r', 'reason': 'stop'},
                                 'owner', con, rt)
        cancel.assert_not_called()

    def test_mcp_start_uses_authenticated_caller_and_arms_runner(self):
        con, rt = object(), object()
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(workflow_run_cli.workflows, 'run_template', return_value={'context': {'instructions': 'original\n'}}) as template, \
             mock.patch.object(workflow_runs, 'start', return_value={'run_id': 'run'}) as start, \
             mock.patch.object(workflow_runs, 'ensure_runner') as runner:
            result = mcp_server.call('workflow_run_start', {'workflow': 'example', 'template': 'delivery', 'context': {'request': 'exact\n'},
                'request_id': 2, 'idempotency_key': 'once'}, 'actual-owner', con, rt)
        self.assertEqual(result['run_id'], 'run')
        template.assert_called_once_with('example', 'delivery', {'request': 'exact\n'})
        self.assertEqual(start.call_args.kwargs['plan'], {'context': {'instructions': 'original\n'}})
        self.assertEqual(start.call_args.kwargs['orch_thread'], 'actual-owner')
        runner.assert_called_once_with(con, rt)

    def test_show_is_read_only_and_not_available_to_desktop_entry(self):
        with mock.patch.object(workflow_runs, 'show', return_value={'state': 'running'}) as show, \
             mock.patch.object(workflow_runs, 'ensure_runner') as runner:
            self.assertEqual(mcp_server.call('workflow_run_show', {'run_id': 'r'}, None, 'db', None),
                             {'state': 'running'})
        show.assert_called_once_with('db', 'r')
        runner.assert_not_called()
        self.assertNotIn('workflow_run_start', {tool['name'] for tool in mcp_server.ENTRY_TOOLS})
        tool = next(tool for tool in mcp_server.TOOLS if tool['name'] == 'workflow_run_show')
        self.assertTrue(tool['annotations']['readOnlyHint'])

    def test_service_skips_home_migrations_and_rejects_busy_loop(self):
        args = cli.build_parser().parse_args(['workflow', 'run', 'serve', '--database', 'profile.db', '--once'])
        self.assertEqual(cli._home_policy(args), 'skip')
        args.interval = 0
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(workflow_run_cli, 'Runtime'), \
             mock.patch.object(workflow_runs, 'serve') as serve, \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(workflow_run_cli.run(args, None), 1)
        serve.assert_not_called()

    def test_report_accepts_a_declared_artifact_without_inline_copy(self):
        args = cli.build_parser().parse_args(['report', 'task', '--status', 'done',
            '--summary', 'prepared', '--output-file', '.local/result.json'])
        self.assertEqual(args.output_file, Path('.local/result.json'))
        self.assertEqual(args.evidence, '')


class WorkflowReplyBoundary(unittest.TestCase):
    def setUp(self):
        from tests.test_workflow_runs import WorkflowRunTests
        self.fixture = WorkflowRunTests('test_exact_output_bytes_bind_next_task_and_final_origin')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.start()

    def test_mcp_cannot_replace_engine_final_with_a_manual_completion(self):
        f = self.fixture
        with self.assertRaisesRegex(ValueError, '[Ww]orkflow|[Rr]un'):
            mcp_server.call('interface_reply', {'request_id': f.request_id, 'text': 'premature done',
                            'kind': 'final'}, 'example-orch', f.con, f.rt)
        row = f.con.execute('SELECT reply FROM interface_requests WHERE id=?', (f.request_id,)).fetchone()
        self.assertIsNone(row['reply'])

    def test_shared_facade_cannot_dismiss_a_request_owned_by_a_run(self):
        from orchd.channels import service
        f = self.fixture
        with self.assertRaisesRegex(ValueError, '[Ww]orkflow|[Rr]un'):
            service.legacy_interface(f.con, 'dismiss', actor='example-orch',
                                     payload={'request_id': f.request_id, 'reason': 'ignore'})
        row = f.con.execute('SELECT dismissed_at FROM interface_requests WHERE id=?', (f.request_id,)).fetchone()
        self.assertIsNone(row['dismissed_at'])


if __name__ == '__main__':
    unittest.main()
