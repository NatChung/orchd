"""Shared ask contract, legacy compatibility and additive snapshot display evidence."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchd import cli, core, entry, mcp_server, question_summary as summary, snapshot, store
from tests.test_entry import EntryRuntime


class SummaryParsingTests(unittest.TestCase):
    def test_valid_block_and_crlf_preserve_full_body(self):
        expected = summary.EXAMPLE.split('\n---詳細---\n')[0]
        for body in (summary.EXAMPLE, summary.EXAMPLE.replace('\n', '\r\n')):
            self.assertEqual(summary.parse(body), expected)

    def test_boundaries(self):
        for body in (None, '', 'legacy', summary.HEADER, summary.HEADER + '\n---詳細---',
                     summary.HEADER + '\n \n---詳細---', 'preface\n' + summary.EXAMPLE,
                     summary.EXAMPLE.replace('\n---詳細---\n', '\n ---詳細---\n'),
                     summary.EXAMPLE.replace('\n---詳細---\n', '\n---詳細--- \n'),
                     summary.EXAMPLE.replace('\n---詳細---\n', ' ---詳細--- ')):
            with self.subTest(body=body):
                self.assertIsNone(summary.parse(body))
        self.assertEqual(summary.parse(summary.HEADER + '\ntext\n---詳細---'), summary.HEADER + '\ntext')
        self.assertEqual(summary.parse(summary.EXAMPLE + '\n---詳細---\nmore'), summary.parse(summary.EXAMPLE))
        self.assertIn(' ---詳細---', summary.parse(summary.HEADER + '\ntext\n ---詳細---\n---詳細---\nlog'))

    def test_help_and_all_worker_prompts_include_contract_and_example(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            cli.build_parser().parse_args(['ask', '--help'])
        for text in (out.getvalue(), core.worker_brief('orchd'), core.worker_brief('orchd', 'codex'),
                     core.app_brief(), next(t for t in mcp_server.TOOLS if t['name'] == 'ask_nat')['description']):
            self.assertIn(summary.FORMAT, text)
            self.assertIn(summary.EXAMPLE, text)
            self.assertIn('300', text)


class SummaryIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'orchd.db'
        self.con = store.connect(self.path)
        self.addCleanup(self.con.close)
        self.rt = EntryRuntime()
        self.orch = core.start_orch(self.con, self.rt, 'opus')['id']
        entry.bind(self.con, self.rt, self.orch)
        self.task = core.dispatch(self.con, self.rt, orch_thread=self.orch, repo='demo', title='Synthetic',
                                  instructions='i', done_when='d', model='sonnet', model_reason='r', task_type='code')['id']

    def test_cli_warning_accepts_legacy_and_valid_is_quiet(self):
        for body, warns in (('legacy', True), (summary.EXAMPLE, False)):
            out, err = io.StringIO(), io.StringIO()
            with patch('orchd.cli.store.connect', return_value=self.con), patch('orchd.cli.Runtime', return_value=self.rt), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                cli.main(['ask', self.task, body])
            self.assertEqual('warning:' in err.getvalue(), warns)
            self.assertIn('asked on', out.getvalue())
            self.assertEqual(store.latest_message(self.con, self.task, 'question')['body'], body)

    def test_mcp_warning_is_visible_and_body_is_accepted(self):
        for body, warns in (('legacy', True), (summary.EXAMPLE, False)):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                response = mcp_server.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                    'params': {'name': 'ask_nat', 'arguments': {'text': body}, '_meta': {'threadId': self.orch}}},
                    self.con, self.rt)
            self.assertNotIn('isError', response['result'])
            result = json.loads(response['result']['content'][0]['text'])
            self.assertEqual(bool(result.get('warnings')), warns)
            self.assertEqual('warning:' in err.getvalue(), warns)
            self.assertEqual(entry._row(self.con, result['question_id'])['body'], body)

    def test_snapshot_v2_summary_null_and_v1_unchanged(self):
        for body in ('legacy', summary.EXAMPLE):
            store.add_message(self.con, self.task, 'question', body)
            store.update_task(self.con, self.task, status='question')
            record = snapshot.read(self.path, schema_version=2)['pending_worker_questions'][0]
            self.assertEqual(record['summary'], summary.parse(body))
            self.assertEqual(record['body'], body)
            v1 = snapshot.read(self.path, schema_version=1)
            self.assertNotIn('pending_worker_questions', v1)
            self.assertNotIn('summary', v1['worker_questions'][-1])
        self.con.execute("DELETE FROM messages WHERE task_id=? AND kind='question'", (self.task,))
        self.con.commit()
        record = snapshot.read(self.path, schema_version=2)['pending_worker_questions'][0]
        self.assertIsNone(record['summary'])
        self.assertIsNone(record['body'])
