"""Synthetic offline run engine tests; fake workers never start a model or service."""
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from orchd import core, interface, store, verify, workflow_runs as runs, workflows
from tests.test_orchd import FakeRuntime


class RunRuntime(FakeRuntime):
    def __init__(self, folder):
        super().__init__()
        self.folder = folder
        self.created = []
        self.start_error = False

    def repo_path(self, repo):
        return self.folder / repo

    def create_worktree(self, repo_path, repo, task_id):
        path = self.folder / 'worktrees' / (repo + '-' + task_id)
        path.parent.mkdir(exist_ok=True)
        subprocess.run(['git', '-C', str(repo_path), 'worktree', 'add', '-qb', 'orchd/' + task_id, str(path)], check=True, capture_output=True)
        self.created.append(task_id)
        head = subprocess.check_output(['git', '-C', str(path), 'rev-parse', 'HEAD'], text=True).strip()
        return head, 'orchd/' + task_id, str(path)

    def start_worker(self, worktree, sock, brief, model):
        if self.start_error:
            raise RuntimeError('synthetic uncertain start')
        self.brief = brief
        return 'job-' + Path(worktree).name, 'session-' + Path(worktree).name


class WorkflowRunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name).resolve()
        self.env = patch.dict(os.environ, {'ORCHD_CONFIG_DIR': str(self.folder / 'config'), 'ORCHD_HOME': str(self.folder / 'state')})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.rt = RunRuntime(self.folder)
        self.db = self.folder / 'tasks.db'
        self.con = store.connect(self.db)
        self.addCleanup(self.con.close)
        store.register_orch(self.con, 'example-orch', 'claude')
        registration = interface.register(self.con, name='example-interface', orch_id='example-orch', token='synthetic-adapter-token')
        request = interface.submit(self.con, self.rt, name='example-interface', token=registration['token'], conversation='example-chat', request_key='request1', text='Run the scoped synthetic example')
        self.request_id = request['request_id']
        self.interface_token = registration['token']
        for name in ('delivery-service', 'reminder-service'):
            root = self.folder / name
            root.mkdir()
            self.git(root, 'init', '-q')
            (root / '.gitignore').write_text('.gstack/\n')
            (root / 'src').mkdir()
            (root / 'src/example.txt').write_text('synthetic source\n')
            self.git(root, 'add', '-A')
            self.git(root, '-c', 'user.name=Example', '-c', 'user.email=example@example.invalid', 'commit', '-qm', 'Synthetic fixture')
        source = self.folder / 'example-workflow'
        self.plugin_source = source
        source.mkdir()
        self.git(source, 'init', '-q')
        stages = {}
        for stage, task_type, inputs in [('prepare', 'ops', ['request']), ('implement', 'code', ['instructions']), ('review', 'review', [])]:
            stages[stage] = dict(task_type=task_type, inputs=inputs, instructions_file=stage + '.md', done_when_file=stage + '-done.md')
            (source / (stage + '.md')).write_text('Declared task. ' + ' '.join('{{' + i + '}}' for i in inputs))
            (source / (stage + '-done.md')).write_text('Report the declared scoped output.')
        manifest = dict(schema_version='orchd-workflow/v1', name='example-workflow', description='Synthetic workflow', stages=stages)
        (source / 'orchd-workflow.json').write_bytes(workflows.canonical(manifest))
        self.git(source, 'add', '-A')
        self.git(source, '-c', 'user.name=Example', '-c', 'user.email=example@example.invalid', 'commit', '-qm', 'Synthetic plugin')
        pin = workflows.install(source, self.git(source, 'rev-parse', 'HEAD'))
        self.plan = dict(schema_version='orchd-run-plan/v1', workflow={k: pin[k] for k in ('name', 'commit', 'digest')}, context={'request': 'exact request\n'}, steps=[
            dict(id='prepare', repo='delivery-service', stage='prepare', task_type='ops', model='sonnet', model_reason='Scoped fixture', instructions='Prepare only', done_when='Output retained', inputs={'request': {'context': 'request'}}, outputs=['implementation_instructions']),
            dict(id='implement', repo='reminder-service', stage='implement', task_type='code', model='sonnet', model_reason='Scoped fixture', instructions='Implement only', done_when='Output retained', inputs={'instructions': {'output': {'step': 'prepare', 'field': 'implementation_instructions'}}}, outputs=[])
        ])

    def git(self, root, *args):
        return subprocess.check_output(['git', '-C', str(root), *args], text=True, stderr=subprocess.PIPE).strip()

    def start(self, plan=None):
        return runs.start(self.con, self.rt, plan=plan or self.plan, request_id=self.request_id, orch_thread='example-orch', idempotency_key='same-request')['run_id']

    def tick(self, run_id, count=1):
        for _ in range(count):
            runs.reconcile(self.con, self.rt, run_id)
        return runs.show(self.con, run_id)

    def output(self, task_id, values):
        task = store.get_task(self.con, task_id)
        path = Path(task['worktree']) / '.gstack/output.json'
        path.parent.mkdir(exist_ok=True)
        raw = json.dumps(dict(schema_version=runs.OUTPUT_SCHEMA, values=values), ensure_ascii=False).encode()
        path.write_bytes(raw)
        core.report(self.con, self.rt, task_id, 'done', 'Self-reported result', 'Synthetic evidence', output_file='.gstack/output.json')
        return path, raw

    def test_exact_output_bytes_bind_next_task_and_final_origin(self):
        run_id = self.start()
        first = self.tick(run_id)['attempts'][0]['task_id']
        value = 'long original instructions\n保留最後換行\n'
        path, raw = self.output(first, {'implementation_instructions': value})
        path.write_text('changed after capture')
        state = self.tick(run_id, 2)
        second = state['attempts'][1]['task_id']
        task = store.get_task(self.con, second)
        record = json.loads(self.con.execute("SELECT body FROM messages WHERE task_id=? AND kind='dispatch'", (second,)).fetchone()[0])
        self.assertEqual(record['workflow']['inputs']['instructions'], value)
        self.assertEqual(task['repo'], 'reminder-service')
        self.assertEqual(store.get_task(self.con, first)['status'], 'done')
        with self.assertRaisesRegex(ValueError, 'retained'):
            core.close(self.con, self.rt, first)
        self.assertEqual(self.con.execute('SELECT output FROM workflow_run_attempts WHERE task_id=?', (first,)).fetchone()[0], raw)
        self.output(second, {})
        self.assertEqual(self.tick(run_id, 2)['state'], 'done')
        reply = self.con.execute('SELECT interface_id,conversation,reply FROM interface_requests WHERE id=?', (self.request_id,)).fetchone()
        self.assertEqual(reply['conversation'], 'example-chat')
        self.assertIn(run_id, reply['reply'])
        self.assertEqual(self.start(), run_id)
        self.assertEqual(len(self.rt.created), 2)

    def test_missing_output_and_symlink_do_not_record_done(self):
        run_id = self.start()
        task_id = self.tick(run_id)['attempts'][0]['task_id']
        with self.assertRaisesRegex(ValueError, 'requires output_file'):
            core.report(self.con, self.rt, task_id, 'done', 'summary', 'evidence')
        task = store.get_task(self.con, task_id)
        link = Path(task['worktree']) / '.gstack'
        outside = self.folder / 'outside'
        outside.mkdir()
        link.symlink_to(outside, target_is_directory=True)
        (outside / 'output.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'symlink'):
            core.report(self.con, self.rt, task_id, 'done', 'summary', 'evidence', output_file='.gstack/output.json')
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM messages WHERE task_id=? AND kind='report'", (task_id,)).fetchone()[0], 0)

    def test_idempotency_and_wrong_origin_fail_before_tasks(self):
        run_id = self.start()
        self.assertEqual(self.start(), run_id)
        altered = copy.deepcopy(self.plan)
        altered['context']['request'] += '\n'
        with self.assertRaisesRegex(ValueError, 'conflict'):
            self.start(altered)
        with self.assertRaisesRegex(ValueError, 'owned'):
            runs.start(self.con, self.rt, plan=self.plan, request_id=self.request_id, orch_thread='different-orch', idempotency_key='same-request')
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM tasks').fetchone()[0], 0)

    def test_wrong_pin_repo_alias_and_missing_input_rejected_before_tasks(self):
        for mutate in ('pin', 'repo', 'inputs'):
            plan = copy.deepcopy(self.plan)
            if mutate == 'pin':
                plan['workflow']['digest'] = 'sha256:' + 'a' * 64
            elif mutate == 'repo':
                plan['steps'][0]['repo'] = '/absolute/alias'
            else:
                plan['steps'][0]['inputs'] = {}
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                self.start(plan)
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM tasks').fetchone()[0], 0)

    def test_uncertain_spawn_never_creates_second_task(self):
        self.rt.start_error = True
        run_id = self.start()
        self.assertEqual(self.tick(run_id)['state'], 'needs_recovery')
        count = self.con.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]
        self.tick(run_id, 3)
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM tasks').fetchone()[0], count)
        self.assertEqual(len(self.rt.created), 1)

    def test_crash_after_launch_claim_is_not_blindly_replayed(self):
        run_id = self.start()
        run = runs._get(self.con, run_id)
        with store.immediate(self.con):
            attempt = runs._reserve(self.con, run, self.plan['steps'][0], 'author')
            self.con.execute("UPDATE workflow_run_attempts SET state='launching' WHERE id=?", (attempt['id'],))
        self.assertEqual(self.tick(run_id)['state'], 'needs_recovery')
        self.assertEqual(self.rt.created, [])

    def test_restart_reconciles_report_without_duplicate_dispatch(self):
        run_id = self.start()
        author = self.tick(run_id)['attempts'][0]['task_id']
        self.output(author, {'implementation_instructions': 'exact bytes'})
        other = store.connect(self.db)
        self.addCleanup(other.close)
        runs.reconcile(other, self.rt, run_id)
        runs.reconcile(other, self.rt, run_id)
        self.assertEqual(len(self.rt.created), 2)
        self.tick(run_id, 2)
        self.assertEqual(len(self.rt.created), 2)

    def test_native_verify_sidecar_runs_before_author_close(self):
        plan = copy.deepcopy(self.plan)
        plan['steps'] = [plan['steps'][1]]
        node = plan['steps'][0]
        node['inputs'] = {'instructions': {'context': 'request'}}
        node['verification'] = dict(repo='reminder-service', stage='review', task_type='review', model='sonnet', model_reason='Independent native check', instructions='Verify the locked head', done_when='Native check passed', inputs={}, outputs=[], paths=['src'], command='python3 -c "print(123)"')
        run_id = self.start(plan)
        author = self.tick(run_id)['attempts'][0]['task_id']
        self.output(author, {})
        core.report(self.con, self.rt, author, 'done', 'Repeated open-author result', 'Same retained output', output_file='.gstack/output.json')
        with self.assertRaisesRegex(ValueError, 'before independent'):
            core.close(self.con, self.rt, author)
        state = self.tick(run_id)
        self.assertEqual(len(state['attempts']), 2)
        reviewer = next(a['task_id'] for a in state['attempts'] if a['role'] == 'reviewer')
        self.assertEqual(store.get_task(self.con, reviewer)['verifies'], author)
        self.assertNotEqual(store.get_task(self.con, author)['status'], 'closed')
        result = verify.run(self.con, reviewer)
        self.assertTrue(result['passed'])
        self.output(reviewer, {})
        self.assertEqual(self.tick(run_id, 2)['state'], 'done')

    def verification(self, command='python3 -c "print(123)"'):
        return dict(repo='reminder-service', stage='review', task_type='review', model='sonnet', model_reason='Independent verification', instructions='Check locked head', done_when='Native verify and output reported', inputs={}, outputs=[], paths=['src'], command=command)

    def test_blocked_automatically_starts_one_bounded_recovery_and_needs_adoption(self):
        recovery = copy.deepcopy(self.plan['steps'][1])
        recovery.update(id='repair', inputs={'instructions': {'context': 'failure'}}, verification=self.verification())
        plan = copy.deepcopy(self.plan)
        plan['failure_steps'] = [recovery]
        parent = self.start(plan)
        failed_task = self.tick(parent)['attempts'][0]['task_id']
        core.report(self.con, self.rt, failed_task, 'blocked', 'A real check found a problem', 'Synthetic negative case')
        parent_state = runs.show(self.con, parent)
        self.assertEqual(parent_state['state'], 'repairing')
        repair = parent_state['repair_run_id']
        runs.reconcile(self.con, self.rt)
        repair_task = runs.show(self.con, repair)['attempts'][0]['task_id']
        metadata = json.loads(self.con.execute("SELECT body FROM messages WHERE task_id=? AND kind='dispatch'", (repair_task,)).fetchone()[0])
        finding = json.loads(metadata['workflow']['inputs']['instructions'])
        self.assertEqual(finding['source_task'], failed_task)
        self.assertEqual(finding['validated_outputs'], {})
        self.assertEqual(finding['task_refs'][0]['task_id'], failed_task)
        self.assertIn('not independent acceptance', finding['basis'])
        self.assertIsInstance(finding['source_message_id'], int)
        self.output(repair_task, {})
        state = self.tick(repair)
        reviewer = next(a['task_id'] for a in state['attempts'] if a['role'] == 'reviewer')
        self.assertTrue(verify.run(self.con, reviewer)['passed'])
        self.output(reviewer, {})
        self.assertEqual(self.tick(repair, 2)['state'], 'needs_adoption')
        self.assertEqual(runs.show(self.con, parent)['state'], 'needs_adoption')
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM workflow_runs WHERE parent_run_id=?', (parent,)).fetchone()[0], 1)
        reply = self.con.execute('SELECT reply FROM interface_requests WHERE id=?', (self.request_id,)).fetchone()[0]
        self.assertIn('needs adoption', reply)
        self.assertNotIn('finished its declared steps', reply)

    def test_recovery_failure_is_bounded_not_recursive(self):
        repair_node = copy.deepcopy(self.plan['steps'][1])
        repair_node.update(id='repair', inputs={'instructions': {'context': 'failure'}}, verification=self.verification())
        plan = copy.deepcopy(self.plan)
        plan['failure_steps'] = [repair_node]
        parent = self.start(plan)
        author = self.tick(parent)['attempts'][0]['task_id']
        core.report(self.con, self.rt, author, 'blocked', 'Failed', 'Negative case')
        child = runs.show(self.con, parent)['repair_run_id']
        task = self.tick(child)['attempts'][0]['task_id']
        core.report(self.con, self.rt, task, 'blocked', 'Repair failed', 'Negative case')
        runs.reconcile(self.con, self.rt)
        self.assertEqual(runs.show(self.con, parent)['state'], 'blocked')
        self.assertEqual(runs.show(self.con, child)['state'], 'blocked')
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM workflow_runs').fetchone()[0], 2)

    def test_native_failure_creates_recovery_without_manual_start(self):
        plan = copy.deepcopy(self.plan)
        node = plan['steps'][1]
        node['inputs'] = {'instructions': {'context': 'request'}}
        node['verification'] = self.verification('exit 1')
        plan['steps'] = [node]
        repair = copy.deepcopy(node)
        repair.update(id='repair', inputs={'instructions': {'context': 'failure'}}, verification=self.verification())
        plan['failure_steps'] = [repair]
        parent = self.start(plan)
        author = self.tick(parent)['attempts'][0]['task_id']
        self.output(author, {})
        reviewer = next(a['task_id'] for a in self.tick(parent)['attempts'] if a['role'] == 'reviewer')
        self.assertFalse(verify.run(self.con, reviewer)['passed'])
        state = self.tick(parent)
        self.assertEqual(state['state'], 'repairing')
        self.assertIsNotNone(state['repair_run_id'])

    def test_task_metadata_is_core_owned_not_copied_from_worker_output(self):
        plan = copy.deepcopy(self.plan)
        plan['steps'][1]['inputs']['instructions'] = {'task': {'step': 'prepare', 'field': 'task_id'}}
        run_id = self.start(plan)
        first = self.tick(run_id)['attempts'][0]['task_id']
        self.output(first, {'implementation_instructions': 'untrusted pretend task ID'})
        second = self.tick(run_id, 2)['attempts'][1]['task_id']
        data = json.loads(self.con.execute("SELECT body FROM messages WHERE task_id=? AND kind='dispatch'", (second,)).fetchone()[0])
        self.assertEqual(data['workflow']['inputs']['instructions'], first)

    def test_restart_migrates_processing_owner_without_changing_reply_origin(self):
        run_id = self.start()
        task = self.tick(run_id)['attempts'][0]['task_id']
        store.register_orch(self.con, 'replacement-orch', 'claude')
        with store.immediate(self.con):
            interface.migrate_owner(self.con, 'example-orch', 'replacement-orch')
            runs.migrate_owner(self.con, 'example-orch', 'replacement-orch')
        self.assertEqual(store.get_task(self.con, task)['orch_thread'], 'replacement-orch')
        self.output(task, {'implementation_instructions': 'same original input'})
        next_task = self.tick(run_id, 2)['attempts'][1]['task_id']
        self.assertEqual(store.get_task(self.con, next_task)['orch_thread'], 'replacement-orch')
        self.output(next_task, {})
        self.assertEqual(self.tick(run_id, 2)['state'], 'done')
        row = self.con.execute('SELECT conversation,orch_id FROM interface_requests WHERE id=?', (self.request_id,)).fetchone()
        self.assertEqual(tuple(row), ('example-chat', 'replacement-orch'))

    def test_registry_upgrade_does_not_change_frozen_run_pin_or_text(self):
        run_id = self.start()
        source = self.plugin_source
        (source / 'prepare.md').write_text('Newer plugin instructions must not replace the frozen pin.')
        self.git(source, 'add', '-A')
        self.git(source, '-c', 'user.name=Example', '-c', 'user.email=example@example.invalid', 'commit', '-qm', 'New synthetic version')
        workflows.install(source, self.git(source, 'rev-parse', 'HEAD'))
        task_id = self.tick(run_id)['attempts'][0]['task_id']
        task = store.get_task(self.con, task_id)
        self.assertNotIn('Newer plugin instructions', task['instructions'])
        record = json.loads(self.con.execute("SELECT body FROM messages WHERE task_id=? AND kind='dispatch'", (task_id,)).fetchone()[0])
        self.assertEqual(record['workflow']['commit'], self.plan['workflow']['commit'])

    def test_internal_reservation_cannot_be_injected_through_json_dispatch(self):
        with self.assertRaisesRegex(ValueError, 'internal'):
            core.dispatch(self.con, self.rt, orch_thread='example-orch', repo='reminder-service', title='Fake', instructions='Fake', done_when='Fake', model='sonnet', model_reason='Synthetic attempt', task_type='code', _run_attempt='guessed-json-reservation')
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM tasks').fetchone()[0], 0)

    def test_attention_is_delivered_to_original_origin_not_only_event_log(self):
        self.rt.start_error = True
        run_id = self.start()
        self.assertEqual(self.tick(run_id)['state'], 'needs_recovery')
        row = self.con.execute('SELECT conversation,reply FROM interface_requests WHERE id=?', (self.request_id,)).fetchone()
        self.assertEqual(row['conversation'], 'example-chat')
        self.assertIn('needs_recovery', row['reply'])
        self.assertIn('not completed', row['reply'])
        self.tick(run_id)
        self.assertEqual(self.con.execute('SELECT reply FROM interface_requests WHERE id=?', (self.request_id,)).fetchone()[0], row['reply'])

    def test_question_routes_to_original_interim_and_desktop_guard_is_structural(self):
        run_id = self.start()
        task = self.tick(run_id)['attempts'][0]['task_id']
        core.ask(self.con, self.rt, task, 'Which declared input should be used?')
        self.assertEqual(self.tick(run_id)['state'], 'awaiting_input')
        row = self.con.execute('SELECT interim,reply FROM interface_requests WHERE id=?', (self.request_id,)).fetchone()
        self.assertIn('Which declared input', row['interim'])
        self.assertIsNone(row['reply'])
        with self.assertRaisesRegex(ValueError, 'origin'):
            runs.guard_desktop_route(self.con, 'example-orch', task)
        with self.assertRaisesRegex(ValueError, 'unscoped'):
            runs.guard_desktop_route(self.con, 'example-orch')
        unrelated = core.dispatch(self.con, self.rt, orch_thread='example-orch', repo='reminder-service', title='Unrelated scoped task', instructions='Only this unrelated task', done_when='Report result', model='sonnet', model_reason='Synthetic non-run case', task_type='code')
        runs.guard_desktop_route(self.con, 'example-orch', unrelated['id'])

    def test_prior_native_pass_becomes_stale_before_next_step(self):
        plan = copy.deepcopy(self.plan)
        node = plan['steps'][1]
        node['inputs'] = {'instructions': {'context': 'request'}}
        node['verification'] = self.verification()
        plan['steps'] = [node]
        run_id = self.start(plan)
        author = self.tick(run_id)['attempts'][0]['task_id']
        self.output(author, {})
        reviewer = next(a['task_id'] for a in self.tick(run_id)['attempts'] if a['role'] == 'reviewer')
        self.assertTrue(verify.run(self.con, reviewer)['passed'])
        worktree = Path(store.get_task(self.con, author)['worktree'])
        (worktree / 'src/example.txt').write_text('Changed after native verification')
        self.git(worktree, 'add', '-A')
        self.git(worktree, '-c', 'user.name=Example', '-c', 'user.email=example@example.invalid', 'commit', '-qm', 'Move synthetic head')
        self.output(reviewer, {})
        self.assertEqual(self.tick(run_id)['state'], 'blocked')
        self.assertEqual(runs.show(self.con, run_id)['step_index'], 0)

    def test_runner_launch_uses_core_package_and_preserves_profile_environment(self):
        self.start()
        with patch('subprocess.Popen') as spawn:
            spawn.return_value.pid = 1234
            result = runs.ensure_runner(self.con, self.rt)
        self.assertEqual(result['state'], 'starting')
        argv = spawn.call_args.args[0]
        self.assertEqual(argv[1:5], ['-P', '-m', 'orchd.cli', 'workflow'])
        self.assertEqual(spawn.call_args.kwargs['env']['ORCHD_CONFIG_DIR'], str(self.folder / 'config'))
        self.assertIn(str(self.db), argv)

    def test_failure_steps_bind_available_parent_output_without_model_copy(self):
        plan = copy.deepcopy(self.plan)
        repair = copy.deepcopy(plan['steps'][1])
        repair.update(id='repair', verification=self.verification())
        plan['failure_steps'] = [repair]
        parent = self.start(plan)
        prepare = self.tick(parent)['attempts'][0]['task_id']
        exact = 'retained parent tool path\n'
        self.output(prepare, {'implementation_instructions': exact})
        author = self.tick(parent, 2)['attempts'][1]['task_id']
        core.report(self.con, self.rt, author, 'blocked', 'Negative check', 'Retained evidence')
        child = runs.show(self.con, parent)['repair_run_id']
        repair_task = self.tick(child)['attempts'][0]['task_id']
        record = json.loads(self.con.execute("SELECT body FROM messages WHERE task_id=? AND kind='dispatch'", (repair_task,)).fetchone()[0])
        self.assertEqual(record['workflow']['inputs']['instructions'], exact)
        failure = json.loads(json.loads(runs._get(self.con, child)['plan'])['context']['failure'])
        self.assertEqual(failure['validated_outputs']['prepare']['implementation_instructions'], exact)
        self.assertEqual(failure['output_provenance']['prepare']['task_id'], prepare)

    def test_serve_loop_automatically_advances_fast_reported_tasks(self):
        run_id = self.start()
        original = self.rt.start_worker
        def report_before_spawn_returns(worktree, socket, brief, model):
            task_id = self.rt.created[-1]
            attempt = self.con.execute('SELECT spec FROM workflow_run_attempts WHERE task_id=?', (task_id,)).fetchone()
            outputs = json.loads(attempt['spec'])['outputs']
            self.output(task_id, {key: 'Original next-step instructions\n' for key in outputs})
            return original(worktree, socket, brief, model)
        self.rt.start_worker = report_before_spawn_returns
        with patch('orchd.workflow_runs.time.sleep', side_effect=[None] * 6 + [StopIteration]):
            with self.assertRaises(StopIteration):
                runs.serve(self.db, self.rt, interval=0.05)
        self.assertEqual(runs.show(self.con, run_id)['state'], 'done')
        self.assertEqual(len(self.rt.created), 2)
        for task_id in self.rt.created:
            self.assertEqual(store.get_task(self.con, task_id)['status'], 'closed')
        self.assertIsNotNone(self.con.execute('SELECT reply FROM interface_requests WHERE id=?', (self.request_id,)).fetchone()[0])

    def test_second_question_is_retained_as_delivery_pending_not_silently_lost(self):
        run_id = self.start()
        task = self.tick(run_id)['attempts'][0]['task_id']
        core.ask(self.con, self.rt, task, 'First scoped question?')
        self.tick(run_id)
        core.ask(self.con, self.rt, task, 'Second scoped question?')
        self.tick(run_id)
        events = runs.events(self.con, run_id)
        self.assertTrue(any(e['kind'] == 'origin_delivery_pending' for e in events))
        row = self.con.execute('SELECT interim,reply FROM interface_requests WHERE id=?', (self.request_id,)).fetchone()
        self.assertIn('First scoped question', row['interim'])
        self.assertIsNone(row['reply'])

    def test_no_declared_outputs_needs_no_worker_generated_noop_artifact(self):
        plan = copy.deepcopy(self.plan)
        node = plan['steps'][1]
        node['inputs'] = {'instructions': {'context': 'request'}}
        plan['steps'] = [node]
        run_id = self.start(plan)
        task_id = self.tick(run_id)['attempts'][0]['task_id']
        core.report(self.con, self.rt, task_id, 'done', 'Self report', 'No output fields')
        row = self.con.execute('SELECT output FROM workflow_run_attempts WHERE task_id=?', (task_id,)).fetchone()
        self.assertEqual(json.loads(row['output']), {'schema_version': runs.OUTPUT_SCHEMA, 'values': {}})
        event = next(e for e in runs.events(self.con, run_id) if e['kind'] == 'report')
        self.assertEqual(json.loads(event['body'])['output_basis'], 'no_declared_outputs_task_self_report')
        self.assertEqual(self.tick(run_id, 2)['state'], 'done')

    def test_manual_interface_reply_cannot_preempt_engine_owned_request(self):
        run_id = self.start()
        self.tick(run_id)
        for kind in ('interim', 'final'):
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, 'engine owns this source route'):
                runs.guard_interface_reply(self.con, self.request_id, 'example-orch')
                interface.reply(self.con, 'example-orch', request_id=self.request_id, text='Premature manual reply', kind=kind)
        row = self.con.execute('SELECT interim,reply FROM interface_requests WHERE id=?', (self.request_id,)).fetchone()
        self.assertIsNone(row['interim'])
        self.assertIsNone(row['reply'])
        with self.assertRaisesRegex(ValueError, 'not owned'):
            runs.guard_interface_reply(self.con, self.request_id, 'different-orch')

    def test_recovery_child_keeps_the_same_request_under_engine_ownership(self):
        repair = copy.deepcopy(self.plan['steps'][1])
        repair.update(id='repair', inputs={'instructions': {'context': 'failure'}}, verification=self.verification())
        plan = copy.deepcopy(self.plan)
        plan['failure_steps'] = [repair]
        parent = self.start(plan)
        author = self.tick(parent)['attempts'][0]['task_id']
        core.report(self.con, self.rt, author, 'blocked', 'Negative check', 'Source evidence')
        child = runs.show(self.con, parent)['repair_run_id']
        child_task = self.tick(child)['attempts'][0]['task_id']
        self.assertEqual(runs.show(self.con, child)['request_id'], self.request_id)
        with self.assertRaisesRegex(ValueError, 'engine owns this source route'):
            runs.guard_interface_reply(self.con, self.request_id, 'example-orch')
        with self.assertRaisesRegex(ValueError, 'before independent'):
            core.close(self.con, self.rt, child_task)
        self.assertIsNone(self.con.execute('SELECT reply FROM interface_requests WHERE id=?', (self.request_id,)).fetchone()[0])

    def test_nonrun_interface_reply_and_completed_idempotent_reply_remain_compatible(self):
        request = interface.submit(self.con, self.rt, name='example-interface', token=self.interface_token, conversation='unrelated-chat', request_key='unrelated', text='Unrelated legacy request')
        runs.guard_interface_reply(self.con, request['request_id'], 'example-orch')
        interface.reply(self.con, 'example-orch', request_id=request['request_id'], text='Legacy reply')
        self.assertEqual(self.con.execute('SELECT reply FROM interface_requests WHERE id=?', (request['request_id'],)).fetchone()[0], 'Legacy reply')
        run_id = self.start()
        first = self.tick(run_id)['attempts'][0]['task_id']
        self.output(first, {'implementation_instructions': 'Exact next input'})
        second = self.tick(run_id, 2)['attempts'][1]['task_id']
        core.report(self.con, self.rt, second, 'done', 'Self report', 'No outputs')
        self.assertEqual(self.tick(run_id, 2)['state'], 'done')
        reply = self.con.execute('SELECT reply FROM interface_requests WHERE id=?', (self.request_id,)).fetchone()[0]
        runs.guard_interface_reply(self.con, self.request_id, 'example-orch')
        interface.reply(self.con, 'example-orch', request_id=self.request_id, text=reply)
        with self.assertRaisesRegex(ValueError, 'different content'):
            interface.reply(self.con, 'example-orch', request_id=self.request_id, text='Different reply')

    def test_cancel_wrong_actor_has_no_side_effect(self):
        run_id = self.start()
        task = self.tick(run_id)['attempts'][0]['task_id']
        before = runs.show(self.con, run_id)
        messages = self.con.execute('SELECT COUNT(*) FROM messages').fetchone()[0]
        with self.assertRaisesRegex(ValueError, 'not owned'):
            runs.cancel(self.con, self.rt, run_id, orch_thread='different-orch', reason='Stop')
        self.assertEqual(runs.show(self.con, run_id), before)
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM messages').fetchone()[0], messages)
        self.assertNotEqual(store.get_task(self.con, task)['status'], 'closed')

    def test_cancel_before_first_step_prevents_all_future_dispatch(self):
        run_id = self.start()
        result = runs.cancel(self.con, self.rt, run_id, orch_thread='example-orch', reason='Synthetic operator cancellation')
        self.assertTrue(result['cancelled'])
        self.assertEqual(self.tick(run_id, 3)['state'], 'cancelled')
        self.assertEqual(self.rt.created, [])
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM tasks').fetchone()[0], 0)
        reply = self.con.execute('SELECT reply FROM interface_requests WHERE id=?', (self.request_id,)).fetchone()[0]
        self.assertIn('was cancelled', reply)
        self.assertIn('did not finish successfully', reply)

    def test_cancel_closes_started_recovery_family_but_not_unrelated_task(self):
        plan = copy.deepcopy(self.plan)
        repair = copy.deepcopy(plan['steps'][1])
        repair.update(id='repair', inputs={'instructions': {'context': 'failure'}}, verification=self.verification())
        plan['failure_steps'] = [repair]
        parent = self.start(plan)
        prepare = self.tick(parent)['attempts'][0]['task_id']
        _, retained = self.output(prepare, {'implementation_instructions': 'Original retained producer output'})
        author = self.tick(parent, 2)['attempts'][1]['task_id']
        core.report(self.con, self.rt, author, 'blocked', 'Real failure', 'Source evidence')
        child = runs.show(self.con, parent)['repair_run_id']
        recovery = self.tick(child)['attempts'][0]['task_id']
        unrelated = core.dispatch(self.con, self.rt, orch_thread='example-orch', repo='reminder-service', title='Unrelated task', instructions='Unrelated allowed scope', done_when='Report result', model='sonnet', model_reason='Synthetic control', task_type='code')
        before_tasks = len(self.rt.created)
        result = runs.cancel(self.con, self.rt, child, orch_thread='example-orch', reason='Cancel the operation including its recovery')
        self.assertTrue(result['cancelled'])
        self.assertEqual(result['run_id'], parent)
        for task_id in (prepare, author, recovery):
            task = store.get_task(self.con, task_id)
            self.assertEqual(task['status'], 'closed')
            self.assertEqual(task['outcome'], 'parked')
        self.assertNotEqual(store.get_task(self.con, unrelated['id'])['status'], 'closed')
        self.assertEqual(runs.show(self.con, child)['state'], 'cancelled')
        self.assertEqual(self.con.execute('SELECT output FROM workflow_run_attempts WHERE task_id=?', (prepare,)).fetchone()[0], retained)
        runs.reconcile(self.con, self.rt)
        self.assertEqual(len(self.rt.created), before_tasks)

    def test_repeated_cancel_is_idempotent_and_does_not_add_effects(self):
        run_id = self.start()
        self.tick(run_id)
        first = runs.cancel(self.con, self.rt, run_id, orch_thread='example-orch', reason='First cancellation reason')
        before = (len(self.rt.stopped), len(self.rt.removed), len(runs.events(self.con, run_id)), self.con.execute('SELECT COUNT(*) FROM messages').fetchone()[0])
        again = runs.cancel(self.con, self.rt, run_id, orch_thread='example-orch', reason='A later retry must not rewrite the first reason')
        self.assertEqual(again, first)
        self.assertEqual((len(self.rt.stopped), len(self.rt.removed), len(runs.events(self.con, run_id)), self.con.execute('SELECT COUNT(*) FROM messages').fetchone()[0]), before)

    def test_cancel_intent_is_durable_while_effect_lock_is_busy(self):
        run_id = self.start()
        self.tick(run_id)
        with runs._locked(self.con):
            result = runs.cancel(self.con, self.rt, run_id, orch_thread='example-orch', reason='Stop after in-flight effect')
            self.assertTrue(result['cleanup_pending'])
            self.assertEqual(result['state'], 'cancelling')
        self.assertEqual(self.tick(run_id)['state'], 'cancelled')
        self.assertEqual(len(self.rt.created), 1)

    def test_cancel_retains_existing_error_final_instead_of_rewriting_history(self):
        self.rt.start_error = True
        run_id = self.start()
        self.tick(run_id)
        original = self.con.execute('SELECT reply FROM interface_requests WHERE id=?', (self.request_id,)).fetchone()[0]
        result = runs.cancel(self.con, self.rt, run_id, orch_thread='example-orch', reason='Cancel failed launch')
        self.assertTrue(result['cancelled'])
        self.assertEqual(result['origin_notification'], 'existing_final_retained')
        self.assertEqual(self.con.execute('SELECT reply FROM interface_requests WHERE id=?', (self.request_id,)).fetchone()[0], original)

    def test_late_failure_report_cannot_start_recovery_after_cancel_intent(self):
        plan = copy.deepcopy(self.plan)
        repair = copy.deepcopy(plan['steps'][1])
        repair.update(id='repair', inputs={'instructions': {'context': 'failure'}}, verification=self.verification())
        plan['failure_steps'] = [repair]
        run_id = self.start(plan)
        author = self.tick(run_id)['attempts'][0]['task_id']
        with runs._locked(self.con):
            self.assertTrue(runs.cancel(self.con, self.rt, run_id, orch_thread='example-orch', reason='Stop in-flight operation')['cleanup_pending'])
            with self.assertRaisesRegex(ValueError, 'Terminal workflow'):
                core.report(self.con, self.rt, author, 'blocked', 'Late failure', 'Retained late report')
            self.assertIsNone(runs.show(self.con, run_id)['repair_run_id'])
        self.assertEqual(self.tick(run_id)['state'], 'cancelled')
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM workflow_runs').fetchone()[0], 1)

    def test_cancel_origin_drift_keeps_cleanup_pending_without_retargeting(self):
        run_id = self.start()
        task = self.tick(run_id)['attempts'][0]['task_id']
        with runs._locked(self.con):
            runs.cancel(self.con, self.rt, run_id, orch_thread='example-orch', reason='Stop safely')
        self.con.execute('UPDATE interface_requests SET conversation=? WHERE id=?', ('changed-origin', self.request_id))
        self.assertEqual(self.tick(run_id)['state'], 'cancelling')
        self.assertEqual(store.get_task(self.con, task)['status'], 'closed')
        self.assertIsNone(self.con.execute('SELECT reply FROM interface_requests WHERE id=?', (self.request_id,)).fetchone()[0])
        self.assertTrue(any(e['kind'] == 'cancel_cleanup_pending' for e in runs.events(self.con, run_id)))

    def test_run_report_output_transition_failure_rolls_back_message_and_status(self):
        run_id = self.start()
        task_id = self.tick(run_id)['attempts'][0]['task_id']
        task = store.get_task(self.con, task_id)
        path = Path(task['worktree']) / '.gstack/output.json'
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps({'schema_version': runs.OUTPUT_SCHEMA, 'values': {'implementation_instructions': 'Captured exact output'}}))
        with patch.object(runs, 'observe_report', side_effect=RuntimeError('synthetic transition failure')):
            with self.assertRaisesRegex(RuntimeError, 'transition failure'):
                core.report(self.con, self.rt, task_id, 'done', 'Self report', 'Evidence', output_file='.gstack/output.json')
        self.assertEqual(store.get_task(self.con, task_id)['status'], task['status'])
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM messages WHERE task_id=? AND kind='report'", (task_id,)).fetchone()[0], 0)
        attempt = self.con.execute('SELECT report_id,output FROM workflow_run_attempts WHERE task_id=?', (task_id,)).fetchone()
        self.assertIsNone(attempt['report_id'])
        self.assertIsNone(attempt['output'])
        self.assertFalse(self.con.in_transaction)

    def pending_recovery(self, *, parent_output_binding=False, limits=None):
        plan = copy.deepcopy(self.plan)
        repair = copy.deepcopy(plan['steps'][1])
        repair.update(id='repair', verification=self.verification())
        if not parent_output_binding:
            repair['inputs'] = {'instructions': {'context': 'failure'}}
        plan['failure_steps'] = [repair]
        if limits:
            plan['limits'] = limits
        parent = self.start(plan)
        author = self.tick(parent)['attempts'][0]['task_id']
        core.report(self.con, self.rt, author, 'blocked', 'Prepare could not produce a trusted output', 'Real source message')
        return parent, runs.show(self.con, parent)['repair_run_id']

    def assert_recovery_stopped_at_original_origin(self, parent, child):
        self.assertEqual(runs.show(self.con, child)['state'], 'blocked')
        self.assertEqual(runs.show(self.con, parent)['state'], 'blocked')
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM workflow_runs').fetchone()[0], 2)
        reply = self.con.execute('SELECT conversation,reply FROM interface_requests WHERE id=?', (self.request_id,)).fetchone()
        self.assertEqual(reply['conversation'], 'example-chat')
        self.assertIn('not completed', reply['reply'])
        self.assertIn('Bounded recovery failed', reply['reply'])
        self.assertEqual(len(self.rt.created), 1)
        self.assertTrue(any(e['kind'] == 'recovery_failed' for e in runs.events(self.con, parent)))

    def test_missing_parent_output_blocks_recovery_and_parent_without_inventing_basis(self):
        parent, child = self.pending_recovery(parent_output_binding=True)
        self.tick(child)
        self.assert_recovery_stopped_at_original_origin(parent, child)
        self.assertEqual(runs.show(self.con, child)['attempts'], [])
        failure = json.loads(json.loads(runs._get(self.con, child)['plan'])['context']['failure'])
        self.assertEqual(failure['validated_outputs'], {})

    def test_recovery_deadline_blocks_parent_and_publishes_original_origin_final(self):
        parent, child = self.pending_recovery(limits={'deadline_seconds': 60, 'max_attempts': 1})
        self.con.execute('UPDATE workflow_runs SET created_at=created_at-120 WHERE id=?', (child,))
        self.tick(child)
        self.assert_recovery_stopped_at_original_origin(parent, child)
        self.assertIn('deadline', runs.show(self.con, parent)['note'])

    def test_other_recovery_reconcile_error_cannot_leave_parent_repairing(self):
        parent, child = self.pending_recovery()
        original = self.rt.repo_path
        replacement = self.folder / 'new-registry-root' / 'reminder-service'
        replacement.mkdir(parents=True)
        self.rt.repo_path = lambda name: replacement if name == 'reminder-service' else original(name)
        self.tick(child)
        self.assert_recovery_stopped_at_original_origin(parent, child)
        self.assertIn('Reconcile refused', runs.show(self.con, parent)['note'])

    def test_delivered_answer_then_worker_report_resumes_without_manual_advance(self):
        run_id = self.start()
        task = self.tick(run_id)['attempts'][0]['task_id']
        core.ask(self.con, self.rt, task, 'May I proceed within the existing scope?')
        self.tick(run_id)
        receipt = core.answer(self.con, self.rt, task, 'Proceed within the existing fixed scope', orch_id='example-orch')
        self.assertEqual(receipt['status'], 'delivered')
        self.output(task, {'implementation_instructions': 'Exact next-stage bytes\n'})
        self.assertEqual(runs.show(self.con, run_id)['state'], 'active')
        state = self.tick(run_id, 2)
        self.assertEqual(state['step_index'], 1)
        self.assertEqual(len(state['attempts']), 2)
        event = next(e for e in runs.events(self.con, run_id) if e['kind'] == 'answered_report_resumed')
        proof = json.loads(event['body'])
        self.assertLess(proof['question_id'], proof['answer_id'])
        self.assertLess(proof['answer_id'], proof['report_id'])

    def test_done_without_delivered_answer_stays_awaiting_input(self):
        run_id = self.start()
        task = self.tick(run_id)['attempts'][0]['task_id']
        core.ask(self.con, self.rt, task, 'Unanswered scoped question?')
        with self.assertRaisesRegex(ValueError, 'delivered answer'):
            self.output(task, {'implementation_instructions': 'Premature report'})
        self.assertEqual(self.tick(run_id)['state'], 'awaiting_input')
        self.assertEqual(store.get_task(self.con, task)['status'], 'question')
        self.assertEqual(len(self.rt.created), 1)

    def test_queued_answer_is_not_delivery_and_cannot_resume(self):
        run_id = self.start()
        task = self.tick(run_id)['attempts'][0]['task_id']
        core.ask(self.con, self.rt, task, 'Question pending a reachable worker?')
        def disconnected(*args):
            raise OSError('Synthetic socket unavailable')
        original_send = self.rt.send_uds
        self.rt.send_uds = disconnected
        with self.assertRaises(ValueError):
            core.answer(self.con, self.rt, task, 'This reply is queued, not delivered', orch_id='example-orch')
        self.assertGreater(len(store.pending_answers(self.con, task)), 0)
        with self.assertRaisesRegex(ValueError, 'delivered answer'):
            self.output(task, {'implementation_instructions': 'Premature report'})
        self.assertEqual(self.tick(run_id)['state'], 'awaiting_input')
        self.rt.send_uds = original_send
        delivered = core.answer(self.con, self.rt, task, flush=True, orch_id='example-orch')
        self.assertEqual(delivered['status'], 'delivered')
        self.output(task, {'implementation_instructions': 'Output after the actual queued-answer delivery'})
        self.assertEqual(runs.show(self.con, run_id)['state'], 'active')

    def test_old_answer_does_not_answer_a_later_question_version(self):
        run_id = self.start()
        task = self.tick(run_id)['attempts'][0]['task_id']
        core.ask(self.con, self.rt, task, 'First scoped question?')
        core.answer(self.con, self.rt, task, 'Answer to the first question', orch_id='example-orch')
        core.ask(self.con, self.rt, task, 'Second question still needs an answer?')
        with self.assertRaisesRegex(ValueError, 'latest question'):
            self.output(task, {'implementation_instructions': 'Cannot use the stale answer'})
        self.assertEqual(self.tick(run_id)['state'], 'awaiting_input')

    def test_cancelled_task_late_ask_and_report_preserve_terminal_state(self):
        run_id = self.start()
        task = self.tick(run_id)['attempts'][0]['task_id']
        runs.cancel(self.con, self.rt, run_id, orch_thread='example-orch', reason='Stop')
        messages = self.con.execute('SELECT COUNT(*) FROM messages').fetchone()[0]
        events = len(runs.events(self.con, run_id))
        reply = self.con.execute('SELECT reply FROM interface_requests WHERE id=?', (self.request_id,)).fetchone()[0]
        with self.assertRaisesRegex(ValueError, 'Terminal workflow'):
            core.ask(self.con, self.rt, task, 'Late question after cancellation')
        with self.assertRaisesRegex(ValueError, 'Terminal workflow'):
            core.report(self.con, self.rt, task, 'done', 'Late result', 'Late evidence')
        self.assertEqual(store.get_task(self.con, task)['status'], 'closed')
        self.assertEqual(self.tick(run_id)['state'], 'cancelled')
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM messages').fetchone()[0], messages)
        self.assertEqual(len(runs.events(self.con, run_id)), events)
        self.assertEqual(self.con.execute('SELECT reply FROM interface_requests WHERE id=?', (self.request_id,)).fetchone()[0], reply)
        self.assertEqual(len(self.rt.created), 1)

    def test_answered_recovery_worker_resumes_child_without_restarting_parent_steps(self):
        parent, child = self.pending_recovery()
        task = self.tick(child)['attempts'][0]['task_id']
        core.ask(self.con, self.rt, task, 'May I continue the fixed repair scope?')
        self.assertEqual(runs.show(self.con, parent)['state'], 'awaiting_input')
        core.answer(self.con, self.rt, task, 'Continue the already declared repair scope', orch_id='example-orch')
        self.output(task, {})
        self.assertEqual(runs.show(self.con, child)['state'], 'active')
        self.assertEqual(runs.show(self.con, parent)['state'], 'repairing')
        self.assertEqual(runs.show(self.con, parent)['step_index'], 0)
        self.assertEqual(len(self.tick(child)['attempts']), 2)
        self.assertEqual(len(self.rt.created), 3)

    def test_cancellation_during_report_capture_cannot_reopen_closed_task(self):
        run_id = self.start()
        task_id = self.tick(run_id)['attempts'][0]['task_id']
        task = store.get_task(self.con, task_id)
        path = Path(task['worktree']) / '.gstack/output.json'
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps({'schema_version': runs.OUTPUT_SCHEMA, 'values': {'implementation_instructions': 'Captured before cancel'}}))
        capture = runs.prepare_report
        def cancel_after_capture(*args):
            value = capture(*args)
            runs.cancel(self.con, self.rt, run_id, orch_thread='example-orch', reason='Cancelled during output capture')
            return value
        with patch.object(runs, 'prepare_report', side_effect=cancel_after_capture):
            with self.assertRaisesRegex(ValueError, 'Terminal workflow'):
                core.report(self.con, self.rt, task_id, 'done', 'Late result', 'Evidence', output_file='.gstack/output.json')
        self.assertEqual(store.get_task(self.con, task_id)['status'], 'closed')
        self.assertEqual(runs.show(self.con, run_id)['state'], 'cancelled')
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM messages WHERE task_id=? AND kind='report'", (task_id,)).fetchone()[0], 0)

    def test_memory_database_never_starts_a_process(self):
        con = store.connect(':memory:')
        self.addCleanup(con.close)
        with patch('subprocess.Popen') as spawn:
            self.assertEqual(runs.ensure_runner(con, self.rt)['state'], 'skipped')
            spawn.assert_not_called()

    def test_serve_once_and_effect_lock_do_not_duplicate_worker(self):
        run_id = self.start()
        runs.serve(self.db, self.rt, once=True)
        self.assertEqual(len(self.rt.created), 1)
        with runs._locked(self.con):
            self.assertTrue(runs.reconcile(self.con, self.rt)['busy'])
        self.assertEqual(len(self.rt.created), 1)
