import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from orchd import cli, core, mcp_server, runtime, store, workflows
from tests.test_orchd import FakeRuntime


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name).resolve()
        self.config = self.folder / 'config'
        env = patch.dict(os.environ, {'ORCHD_CONFIG_DIR': str(self.config), 'ORCHD_HOME': str(self.folder / 'state')})
        env.start()
        self.addCleanup(env.stop)
        self.repo = self.folder / 'example-workflow'
        self.repo.mkdir()
        self.git('init', '-q')
        self.manifest = {'schema_version': 'orchd-workflow/v1', 'name': 'example-workflow',
                         'description': 'Instructions for a synthetic reminder service.',
                         'stages': {'implementation': {'task_type': 'code', 'inputs': ['request'],
                                                      'instructions_file': 'stages/implementation.md',
                                                      'done_when_file': 'stages/implementation-done.md'}}}
        (self.repo / 'stages').mkdir()
        (self.repo / 'stages/implementation.md').write_text('Implement {{request}}.\n')
        (self.repo / 'stages/implementation-done.md').write_text('Verify {{request}} with the native checks.\n')
        self.write_manifest()
        self.commit = self.commit_source()

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], stderr=subprocess.PIPE).decode().strip()

    def write_manifest(self):
        (self.repo / 'orchd-workflow.json').write_bytes(workflows.canonical(self.manifest))

    def commit_source(self):
        self.git('add', '-A')
        self.git('-c', 'user.name=Example Maintainer', '-c', 'user.email=maintainer@example.invalid',
                 'commit', '-qm', 'Record synthetic workflow')
        return self.git('rev-parse', 'HEAD')

    def install(self):
        return workflows.install(self.repo, self.commit)

    def cache(self):
        return self.config / 'workflow-plugins/example-workflow' / self.commit

    def connect(self):
        con = store.connect(self.folder / 'tasks.db')
        self.addCleanup(con.close)
        return con

    def dispatch(self, con, rt, **options):
        return core.dispatch(con, rt, orch_thread='example-orch', repo='reminder-service',
                             title='Synthetic task', instructions='Only implement the requested reminder.',
                             done_when='Native checks pass.', model='sonnet', model_reason='Scoped test',
                             task_type='code', **options)

    def options(self):
        return dict(workflow='example-workflow', workflow_stage='implementation',
                    workflow_inputs={'request': 'an opt-in reminder'})

    def test_install_is_fixed_to_commit_idempotent_and_does_not_execute_source(self):
        (self.repo / 'stages/implementation.md').write_text('Dirty working-tree text must not be used.\n')
        (self.repo / 'untrusted.py').write_text('raise RuntimeError("must not execute")\n')
        first = self.install()
        self.assertEqual(self.install(), first)
        rendered = workflows.render('example-workflow', 'implementation', {'request': 'a reminder'})
        self.assertEqual(rendered['instructions'], 'Implement a reminder.\n')
        self.assertEqual(rendered['commit'], self.commit)
        shown = workflows.show('example-workflow')
        self.assertEqual(set(shown['files']), {'orchd-workflow.json', 'stages/implementation.md', 'stages/implementation-done.md'})
        self.assertFalse((self.cache() / 'untrusted.py').exists())

    def test_missing_commit_short_sha_and_symlink_source_rejected_before_install(self):
        for commit in ('0' * 40, self.commit[:7]):
            with self.assertRaises(ValueError):
                workflows.install(self.repo, commit)
        source = self.repo / 'stages/implementation.md'
        source.unlink()
        source.symlink_to('implementation-done.md')
        self.commit = self.commit_source()
        with self.assertRaisesRegex(ValueError, 'regular committed'):
            self.install()
        self.assertFalse(self.config.exists())

    def test_traversal_unknown_fields_missing_file_and_bad_placeholder_fail_closed(self):
        stage = self.manifest['stages']['implementation']
        for change in ({'instructions_file': '../outside.md'}, {'instructions_file': 'missing.md'},
                       {'hook': 'not-supported'}):
            original = dict(stage)
            stage.update(change)
            self.write_manifest()
            self.commit = self.commit_source()
            with self.assertRaises(ValueError):
                self.install()
            stage.clear()
            stage.update(original)
        self.write_manifest()
        (self.repo / 'stages/implementation.md').write_text('Use {{unknown}}.\n')
        self.commit = self.commit_source()
        with self.assertRaisesRegex(ValueError, 'undeclared'):
            self.install()
        self.assertFalse(self.config.exists())

    def test_render_exact_inputs_and_one_pass_text_only(self):
        self.install()
        for inputs in ({}, {'request': 'x', 'extra': 'y'}, {'request': 7}, []):
            with self.subTest(inputs=inputs), self.assertRaises(ValueError):
                workflows.render('example-workflow', 'implementation', inputs)
        value = 'literal {{request}} and $(not-a-command)'
        self.assertEqual(workflows.render('example-workflow', 'implementation', {'request': value})['instructions'],
                         'Implement ' + value + '.\n')
        with self.assertRaisesRegex(ValueError, 'Unknown workflow stage'):
            workflows.render('example-workflow', 'missing', {'request': 'x'})

    def test_empty_stage_inputs_are_explicit_and_supported(self):
        self.manifest['stages']['implementation']['inputs'] = []
        self.write_manifest()
        (self.repo / 'stages/implementation.md').write_text('Read the assigned scope.\n')
        (self.repo / 'stages/implementation-done.md').write_text('Return the requested evidence.\n')
        self.commit = self.commit_source()
        self.install()
        self.assertEqual(workflows.render('example-workflow', 'implementation', {})['instructions'],
                         'Read the assigned scope.\n')

    def test_cross_platform_path_aliases_are_rejected(self):
        for file in ('C:/outside.md', 'stages./outside.md', 'stages /outside.md', 'NUL.md'):
            with self.subTest(file=file), self.assertRaises(ValueError):
                workflows.relative(file)
        stage = self.manifest['stages']['implementation']
        stage['done_when_file'] = 'stages/IMPLEMENTATION.md'
        files = {'orchd-workflow.json': workflows.canonical(self.manifest),
                 'stages/implementation.md': b'Instructions {{request}}',
                 'stages/IMPLEMENTATION.md': b'Criteria {{request}}'}
        with self.assertRaisesRegex(ValueError, 'case-insensitive'):
            workflows.validate_files(files)

    def test_cache_tamper_missing_extra_and_conflicting_reinstall_are_rejected(self):
        self.install()
        file = self.cache() / 'stages/implementation.md'
        original = file.read_bytes()
        file.write_text('modified')
        for action in (lambda: workflows.list_workflows(), self.install):
            with self.assertRaisesRegex(ValueError, 'modified'):
                action()
        file.unlink()
        with self.assertRaises(ValueError):
            workflows.show('example-workflow')
        file.write_bytes(original)
        extra = self.cache() / 'extra.md'
        extra.write_text('unlisted')
        with self.assertRaises(ValueError):
            workflows.show('example-workflow')
        extra.unlink()
        file.unlink()
        file.symlink_to(self.repo / 'stages/implementation.md')
        with self.assertRaisesRegex(ValueError, 'symlink'):
            workflows.show('example-workflow')

    def test_registry_lock_conflict_does_not_replace_other_writers_lock(self):
        self.install()
        lock = self.config / 'workflow-plugins/.registry.lock'
        lock.write_text('another writer')
        with self.assertRaises(FileExistsError):
            self.install()
        self.assertEqual(lock.read_text(), 'another writer')

    def test_bindings_are_explicit_idempotent_and_instance_local(self):
        self.install()
        first = workflows.bind('reminder-service', 'example-workflow')
        self.assertEqual(workflows.bind('reminder-service', 'example-workflow'), first)
        self.manifest['name'] = 'other-workflow'
        self.write_manifest()
        self.commit = self.commit_source()
        self.install()
        with self.assertRaisesRegex(ValueError, '--replace'):
            workflows.bind('reminder-service', 'other-workflow')
        with patch.dict(os.environ, {'ORCHD_CONFIG_DIR': str(self.folder / 'other-config')}):
            self.install()
            workflows.bind('reminder-service', 'other-workflow')
            self.assertEqual(workflows.bindings(), {'default': None, 'repos': {'reminder-service': 'other-workflow'}})
        self.assertEqual(workflows.bindings(), {'default': None, 'repos': {'reminder-service': 'example-workflow'}})
        workflows.bind('reminder-service', 'other-workflow', replace=True)
        self.assertEqual(workflows.list_workflows()['bindings'], {'default': None, 'repos': {'reminder-service': 'other-workflow'}})

    def test_instance_default_applies_to_every_repo_with_explicit_repo_override(self):
        self.install()
        workflows.bind(None, 'example-workflow')
        workflows.bind(None, 'example-workflow')
        con, rt = self.connect(), FakeRuntime()
        with self.assertRaisesRegex(ValueError, 'bound workflow'):
            self.dispatch(con, rt)

        self.dispatch(con, rt, **self.options())
        self.manifest['name'] = 'other-workflow'
        self.write_manifest()
        self.commit = self.commit_source()
        self.install()
        with self.assertRaisesRegex(ValueError, '--replace'):
            workflows.bind(None, 'other-workflow')
        workflows.bind('reminder-service', 'other-workflow')
        with self.assertRaisesRegex(ValueError, 'bound workflow'):
            self.dispatch(con, rt, **self.options())
        self.dispatch(con, rt, **dict(self.options(), workflow='other-workflow'))
        with patch.dict(os.environ, {'ORCHD_CONFIG_DIR': str(self.folder / 'unbound-config')}):
            self.assertEqual(workflows.bindings(), {'default': None, 'repos': {}})
            self.dispatch(con, rt)


    def test_background_orch_mcp_config_preserves_each_profile_binding(self):
        for workflow in ('example-workflow', 'other-workflow'):
            profile = self.folder / ('profile-' + workflow)
            profile.mkdir()
            self.manifest['name'] = workflow
            self.manifest['description'] = 'Synthetic profile for ' + workflow
            self.write_manifest()
            self.commit = self.commit_source()
            variables = {'ORCHD_HOME': str(profile / 'state'), 'ORCHD_ROOT': str(profile),
                         'ORCHD_CONFIG_DIR': str(profile / 'config'), 'ORCHD_ORCH_HOME': str(profile / 'home'),
                         'ORCHD_INTERFACE_HOME': str(profile / 'interface'), 'ORCHD_CHANNEL_HOME': str(profile / 'channel'),
                         'ORCHD_EXECUTABLE': '/synthetic/bin/orchd', 'ORCHD_PROJECTS': str(self.folder / 'projects')}
            with patch.dict(os.environ, variables, clear=True):
                self.install()
                workflows.bind(None, workflow)
                rt = runtime.Runtime()
                with patch.object(rt, 'orch_socket_path', return_value=str(profile / 'o.sock')), \
                        patch.object(rt, 'start_claude', return_value=('fake-job', 'fake-session')) as start, \
                        patch('orchd.paths.orch_images', return_value=None):
                    rt.start_orch('example-orch', 'opus', profile / 'home')
                args = start.call_args.args[3]
                server = json.loads(Path(args[args.index('--mcp-config') + 1]).read_text())['mcpServers']['orchd']
                self.assertEqual({key: server['env'][key] for key in variables}, variables)
            # Simulate the daemon-owned child's explicit MCP env, not the parent's inherited env.
            with patch.dict(os.environ, server['env'], clear=True):
                self.assertEqual(workflows.bindings()['default'], workflow)

    def test_bound_repo_requires_matching_stage_before_task_or_worktree(self):
        self.install()
        workflows.bind('reminder-service', 'example-workflow')
        con, rt = self.connect(), FakeRuntime()
        for options in ({}, dict(self.options(), workflow='other-workflow'),
                        dict(self.options(), workflow_stage=None)):
            with self.subTest(options=options), patch.object(rt, 'create_worktree') as create:
                with self.assertRaisesRegex(ValueError, 'workflow_show'):
                    self.dispatch(con, rt, **options)
                create.assert_not_called()
                self.assertEqual(len(store.open_tasks(con)), 0)
        task = self.dispatch(con, rt, **self.options())
        self.assertIn('Implement an opt-in reminder.', task['instructions'])
        self.assertIn('Only implement the requested reminder.', task['instructions'])
        self.assertIn('Native checks pass.', task['done_when'])
        self.assertIn('Never push directly to the default branch', rt.brief)
        message = json.loads(con.execute("SELECT body FROM messages WHERE task_id=? AND kind='dispatch'", (task['id'],)).fetchone()['body'])
        self.assertEqual(message['workflow']['commit'], self.commit)
        self.assertEqual(message['workflow']['inputs'], {'request': 'an opt-in reminder'})

    def test_unbound_dispatch_unchanged_and_task_type_mismatch_creates_nothing(self):
        self.install()
        con, rt = self.connect(), FakeRuntime()
        task = self.dispatch(con, rt)
        self.assertEqual(task['instructions'], 'Only implement the requested reminder.')
        self.manifest['stages']['implementation']['task_type'] = 'investigation'
        self.write_manifest()
        self.commit = self.commit_source()
        self.install()
        with patch.object(rt, 'create_worktree') as create:
            with self.assertRaisesRegex(ValueError, 'task_type'):
                self.dispatch(con, rt, **self.options())
            create.assert_not_called()
        self.assertEqual(len(store.open_tasks(con)), 1)

    def test_retry_keeps_stored_version_after_new_install(self):
        self.install()
        con, rt = self.connect(), FakeRuntime()
        task = self.dispatch(con, rt, **self.options())
        original = task['instructions']
        (self.repo / 'stages/implementation.md').write_text('New version {{request}}.\n')
        self.commit = self.commit_source()
        self.install()
        rt.exists = lambda path: True
        with patch.object(core, '_stop_old_worker', return_value=None), patch.object(core, '_record_session_usage'):
            core.retry(con, rt, task['id'], 'sonnet', 'Synthetic retry')
        self.assertEqual(store.get_task(con, task['id'])['instructions'], original)
        self.assertIn(original, rt.sent[-1][2])
        self.assertNotIn('New version an opt-in reminder.', rt.sent[-1][2])

    def test_mcp_queries_need_no_database_or_caller_registration_and_batch_carries_workflow(self):
        self.install()
        with patch.object(store, 'register_orch') as register:
            result = mcp_server.handle({'id': 1, 'method': 'tools/call', 'params': {
                'name': 'workflow_show', 'arguments': {'name': 'example-workflow'}, '_meta': {'threadId': 'example-thread'}}}, None, None)
            self.assertFalse(result['result'].get('isError'))
            self.assertEqual(result['result']['structuredContent']['commit'], self.commit)
            register.assert_not_called()
        con, rt = self.connect(), FakeRuntime()
        stage = self.options()
        item = dict(repo='reminder-service', title='Synthetic task', instructions='Scope', done_when='Evidence',
                    model='sonnet', model_reason='Synthetic test', task_type='code', **stage)
        result = mcp_server.call('dispatch_batch', {'tasks': [item]}, 'example-orch', con, rt)
        self.assertEqual(result['tasks'][0]['status'], 'running')
        task = store.get_task(con, result['tasks'][0]['task_id'])
        self.assertIn(self.commit, task['instructions'])

    def test_cli_workflow_commands_skip_database_and_home_migration(self):
        def run(*args):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                code = cli.main(['workflow', *args])
            return code, out.getvalue()
        with patch('orchd.home_migrations.ensure_current') as migrate, patch.object(store, 'connect') as connect:
            self.assertEqual(run('install', str(self.repo), '--commit', self.commit)[0], 0)
            self.assertEqual(run('show', 'example-workflow')[0], 0)
            code, out = run('render', 'example-workflow', 'implementation', '--inputs', '{"request":"a reminder"}')
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)['instructions'], 'Implement a reminder.\n')
            with patch('orchd.cli.Runtime') as runtime:
                runtime.return_value.repo_path.return_value = Path('/projects/reminder-service')
                self.assertEqual(run('bind', '--repo', 'reminder-service', '--workflow', 'example-workflow')[0], 0)
                runtime.return_value.repo_path.assert_called_once_with('reminder-service')
            with patch('orchd.cli.Runtime') as runtime:
                self.assertEqual(run('bind', '--workflow', 'example-workflow')[0], 0)
                runtime.assert_not_called()
            self.assertEqual(run('bindings')[0], 0)
            self.assertEqual(run('list')[0], 0)
            migrate.assert_not_called()
            connect.assert_not_called()
        with patch.dict(os.environ, {'ORCHD_WORKTREE': str(self.folder / 'task')}):
            self.assertEqual(run('install', str(self.repo), '--commit', self.commit)[0], 1)
            self.assertEqual(run('bind', '--repo', 'reminder-service', '--workflow', 'example-workflow')[0], 1)

    def test_cli_parameter_parse_errors_do_not_echo_values(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            cli.build_parser().parse_args(['workflow', 'render', 'example-workflow', 'implementation',
                                          '--inputs', '{"request":"synthetic-sensitive-value",'])
        self.assertNotIn('synthetic-sensitive-value', err.getvalue())

    def test_cli_dispatch_carries_stage_options_to_the_worker(self):
        self.install()
        con, rt = self.connect(), FakeRuntime()
        store.register_orch(con, 'example-orch', 'codex')
        out = io.StringIO()
        with patch.object(store, 'connect', return_value=con), patch('orchd.cli.Runtime', return_value=rt), \
                patch('orchd.home_migrations.ensure_current', return_value=[]), contextlib.redirect_stdout(out):
            code = cli.main(['dispatch', '--orch-id', 'example-orch', '--repo', 'reminder-service',
                             '--title', 'Synthetic task', '--instructions', 'Caller scope', '--done-when', 'Evidence',
                             '--model', 'sonnet', '--model-reason', 'Synthetic test', '--task-type', 'code',
                             '--workflow', 'example-workflow', '--workflow-stage', 'implementation',
                             '--workflow-inputs', '{"request":"an opt-in reminder"}'])
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn('Implement an opt-in reminder.', rt.sent[-1][2])
        self.assertIn(self.commit, rt.sent[-1][2])

    def test_path_alias_cannot_bypass_repo_binding(self):
        self.install()
        workflows.bind('reminder-service', 'example-workflow')
        con, rt = self.connect(), FakeRuntime()
        with patch.object(rt, 'repo_path', return_value=Path('/projects/reminder-service')):
            with self.assertRaisesRegex(ValueError, 'path alias'):
                core.dispatch(con, rt, orch_thread='example-orch', repo='other/../reminder-service',
                              title='Synthetic task', instructions='Scope', done_when='Evidence',
                              model='sonnet', model_reason='Synthetic test', task_type='code')
        self.assertEqual(len(store.open_tasks(con)), 0)


if __name__ == '__main__':
    unittest.main()
