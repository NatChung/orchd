"""Committed synthetic templates remain data; only declared context can change."""
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from orchd import interface, store, workflow_runs, workflows
from tests.test_workflow_runs import RunRuntime


class RunTemplateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name).resolve()
        env = patch.dict(os.environ, {'ORCHD_CONFIG_DIR': str(self.folder / 'config'), 'ORCHD_HOME': str(self.folder / 'state')})
        env.start()
        self.addCleanup(env.stop)
        self.source = self.folder / 'example-workflow'
        self.source.mkdir()
        self.git('init', '-q')
        (self.source / 'stages').mkdir()
        (self.source / 'run-plans').mkdir()
        (self.source / 'stages/prepare.md').write_text('Use {{request}} without changing its bytes.')
        (self.source / 'stages/prepare-done.md').write_text('Report the declared result.')
        self.manifest = dict(schema_version='orchd-workflow/v1', name='example-workflow', description='Synthetic declarative workflow', stages={
            'prepare': dict(task_type='ops', inputs=['request'], instructions_file='stages/prepare.md', done_when_file='stages/prepare-done.md')},
            run_templates={'example': 'run-plans/example.template.json'})
        self.template = dict(schema_version='orchd-run-template/v1', context={'request': 'default 原文\n'}, limits={'max_attempts': 1, 'deadline_seconds': 3600}, steps=[
            dict(id='prepare', repo='reminder-service', stage='prepare', task_type='ops', model='sonnet', model_reason='Scoped fixture', instructions='Only the declared work', done_when='Report result', inputs={'request': {'context': 'request'}}, outputs=[])])
        self.write()
        self.commit = self.record()

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.source), *args], text=True, stderr=subprocess.PIPE).strip()

    def write(self):
        (self.source / 'orchd-workflow.json').write_bytes(workflows.canonical(self.manifest))
        (self.source / 'run-plans/example.template.json').write_bytes(workflows.canonical(self.template))

    def record(self):
        self.git('add', '-A')
        self.git('-c', 'user.name=Example', '-c', 'user.email=example@example.invalid', 'commit', '-qm', 'Record synthetic run template')
        return self.git('rev-parse', 'HEAD')

    def install(self):
        return workflows.install(self.source, self.commit)

    def test_installed_receipt_supplies_pin_and_exact_context_defaults(self):
        pin = self.install()
        plan = workflows.run_template('example-workflow', 'example', {})
        self.assertEqual(plan['schema_version'], 'orchd-run-plan/v1')
        self.assertEqual(plan['workflow'], pin)
        self.assertEqual(plan['context'], self.template['context'])
        self.assertEqual(plan['steps'], self.template['steps'])
        shown = workflows.show('example-workflow')
        self.assertEqual(shown['manifest']['run_templates'], self.manifest['run_templates'])
        self.assertIn('run-plans/example.template.json', shown['files'])
        self.assertNotIn('workflow', json.loads((self.source / 'run-plans/example.template.json').read_text()))
        self.assertFalse((self.folder / 'state').exists())

    def test_committed_bytes_ignore_dirty_source_and_context_is_never_executed(self):
        original = copy.deepcopy(self.template)
        self.template['steps'][0]['repo'] = 'dirty-source-must-not-be-read'
        self.write()
        self.install()
        value = 'exact Unicode 原文\n$(touch should-not-exist) {{request}}\n'
        plan = workflows.run_template('example-workflow', 'example', {'request': value})
        self.assertEqual(plan['context']['request'], value)
        self.assertEqual(plan['steps'], original['steps'])
        self.assertFalse((self.folder / 'should-not-exist').exists())
        plan['steps'][0]['repo'] = 'caller-mutation'
        self.assertEqual(workflows.run_template('example-workflow', 'example', {})['steps'], original['steps'])

    def test_unknown_keys_nonstring_overrides_and_unknown_templates_fail_closed(self):
        self.install()
        for context in ({'undeclared': 'x'}, {'steps': 'overwrite graph'}, {'request': {'nested': 'object'}}, {'request': '\ud800'}, []):
            with self.subTest(context=context), self.assertRaises(ValueError):
                workflows.run_template('example-workflow', 'example', context)
        with self.assertRaisesRegex(ValueError, 'Unknown installed run template'):
            workflows.run_template('example-workflow', 'missing', {})

    def test_run_template_mapping_paths_and_names_are_validated(self):
        for mapping in ({'../alias': 'run-plans/example.template.json'}, {'example': '../outside.json'},
                        {'example': 'script.py'}, [], {'example': 'missing.json'}):
            manifest = copy.deepcopy(self.manifest)
            manifest['run_templates'] = mapping
            (self.source / 'orchd-workflow.json').write_bytes(workflows.canonical(manifest))
            self.commit = self.record()
            with self.subTest(mapping=mapping), self.assertRaises(ValueError):
                self.install()

    def test_self_pin_hooks_invalid_json_and_nonstring_defaults_are_rejected(self):
        path = self.source / 'run-plans/example.template.json'
        variants = [dict(self.template, workflow={'commit': 'a' * 40}), dict(self.template, hook='execute nothing'),
                    dict(self.template, context={'request': 1}), dict(self.template, steps=[])]
        for value in variants:
            path.write_bytes(workflows.canonical(value))
            self.commit = self.record()
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.install()
        for raw in (b'{"schema_version":"x","schema_version":"y"}', b'not JSON', b'\xff'):
            path.write_bytes(raw)
            self.commit = self.record()
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                self.install()

    def test_symlink_template_is_not_a_regular_committed_blob(self):
        path = self.source / 'run-plans/example.template.json'
        path.unlink()
        path.symlink_to('../stages/prepare.md')
        self.commit = self.record()
        with self.assertRaisesRegex(ValueError, 'regular committed blobs'):
            self.install()

    def test_cache_template_tamper_or_missing_bytes_fail_on_resolution(self):
        self.install()
        path = self.folder / 'config/workflow-plugins/example-workflow' / self.commit / 'run-plans/example.template.json'
        original = path.read_bytes()
        path.write_bytes(original + b'\n')
        with self.assertRaises(ValueError):
            workflows.run_template('example-workflow', 'example', {})
        path.unlink()
        with self.assertRaises(ValueError):
            workflows.run_template('example-workflow', 'example', {})

    def test_legacy_manifest_and_resolved_plan_use_existing_engine_validation(self):
        pin = self.install()
        runtime = RunRuntime(self.folder)
        (self.folder / 'reminder-service').mkdir()
        con = store.connect(self.folder / 'tasks.db')
        self.addCleanup(con.close)
        store.register_orch(con, 'example-orch', 'claude')
        registration = interface.register(con, name='example-interface', orch_id='example-orch')
        request = interface.submit(con, runtime, name='example-interface', token=registration['token'], conversation='example-chat', request_key='template-request', text='Run the fixed template')
        plan = workflows.run_template('example-workflow', 'example', {'request': 'original\n'})
        run = workflow_runs.start(con, runtime, plan=plan, request_id=request['request_id'], orch_thread='example-orch', idempotency_key='template')
        self.assertEqual(run['workflow'], pin)
        self.assertEqual(con.execute('SELECT COUNT(*) FROM tasks').fetchone()[0], 0)
        del self.manifest['run_templates']
        self.write()
        self.commit = self.record()
        self.install()
        self.assertEqual(workflows.render('example-workflow', 'prepare', {'request': 'legacy'})['stage'], 'prepare')
        with self.assertRaises(ValueError):
            workflows.run_template('example-workflow', 'example', {})
