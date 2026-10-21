"""Operator migration contract, including durable recovery and concurrent adapters."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from unittest import mock

from orchd import cli, core, interface, store
from tests.test_orchd import FakeRuntime


class InterfaceRebindTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {'ORCHD_HOME': self.tmp.name, 'ORCHD_ORCH_ID': '', 'ORCHD_WORKTREE': ''})
        self.env.start()
        self.con = store.connect()
        self.rt = FakeRuntime()
        store.register_orch(self.con, 'old', 'codex')
        store.register_orch(self.con, 'new', 'claude', socket='/fake/new.sock')
        self.registration = interface.register(self.con, name='adapter', orch_id='old')
        self.auth = {k: self.registration[k] for k in ('name', 'token')}

    def tearDown(self):
        self.con.close()
        self.env.stop()
        self.tmp.cleanup()

    def submit(self, key='one', con=None):
        return interface.submit(con or self.con, self.rt, **self.auth, conversation='route', request_key=key, text='body')['request_id']

    def move(self, **kwargs):
        return interface.rebind(self.con, self.rt, old_id='old', new_id='new', **kwargs)

    def cli(self, *args, payload=None):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), mock.patch('sys.stdin', io.StringIO(json.dumps(payload))), \
                mock.patch.object(cli, 'Runtime', return_value=self.rt):
            code = cli.main(['interface', *args])
        return code, json.loads(output.getvalue())

    def test_preserves_all_state_and_reverse_codex_claude(self):
        pending = self.submit()
        answered = self.submit('answered')
        dismissed = self.submit('dismissed')
        interface.reply(self.con, 'old', request_id=pending, text='working', kind='interim')
        interface.reply(self.con, 'old', request_id=answered, text='done')
        interface.dismiss(self.con, 'old', request_id=dismissed, reason='skip')
        interface.deregister(self.con, **self.auth)
        before = dict(self.con.execute('SELECT * FROM interfaces').fetchone())
        requests_before = [dict(r) for r in self.con.execute('SELECT * FROM interface_requests ORDER BY id')]
        with mock.patch.object(core, '_notify_orch') as notify:
            result = self.move()
        self.assertEqual(result['interfaces_migrated'], 1)
        self.assertEqual(notify.call_args.args[2], 'new')
        self.assertEqual(notify.call_count, 1)  # answered/dismissed requests never wake
        after = dict(self.con.execute('SELECT * FROM interfaces').fetchone())
        self.assertEqual(after, dict(before, orch_id='new'))
        requests_after = [dict(r) for r in self.con.execute('SELECT * FROM interface_requests ORDER BY id')]
        for a, b in zip(requests_before, requests_after):
            for key in a.keys() - {'orch_id', 'wake_at', 'wake_error', 'wake_attempts', 'wake_due_at'}:
                self.assertEqual(a[key], b[key], key)
        self.assertEqual([r['id'] for r in interface.requests(self.con, 'new')], [pending])
        self.assertEqual(interface.requests(self.con, 'old'), [])
        with self.assertRaisesRegex(ValueError, 'not owned'):
            interface.reply(self.con, 'old', request_id=pending, text='stale')
        with self.assertRaisesRegex(ValueError, 'not owned'):
            interface.dismiss(self.con, 'old', request_id=pending, reason='stale')
        with self.assertRaisesRegex(ValueError, 'not owned'):
            interface.ack(self.con, **self.auth, orch_id='old', conversation='route', request_id=answered)
        # Existing adapter credentials remain valid, including after deregistration.
        self.assertEqual(interface.fetch(self.con, **self.auth, conversation='route')[0]['reply'], 'done')
        interface.ack(self.con, **self.auth, conversation='route', request_id=answered)
        interface.rebind(self.con, self.rt, old_id='new', new_id='old')
        self.assertEqual([r['id'] for r in interface.requests(self.con, 'old')], [pending])
        interface.reply(self.con, 'old', request_id=pending, text='restored')
        self.assertEqual(self.rt.stopped, [])
        self.assertEqual(dict(self.con.execute('SELECT * FROM interfaces').fetchone()), before)

    def test_dry_run_and_list_do_not_write_or_expose_credentials(self):
        rid = self.submit()
        interface.reply(self.con, 'old', request_id=rid, text='working', kind='interim')
        final = self.submit('final')
        interface.reply(self.con, 'old', request_id=final, text='answer')
        before = list(self.con.iterdump())
        with mock.patch.object(core, '_notify_orch') as notify:
            code, preview = self.cli('rebind', '--from', 'old', '--to', 'new', '--dry-run')
        self.assertEqual((code, preview['status'], preview['unanswered']), (0, 'dry-run', 1))
        notify.assert_not_called()
        code, rows = self.cli('list')
        self.assertEqual(code, 0)
        self.assertEqual((rows[0]['unanswered'], rows[0]['pending_final'], rows[0]['pending_interim']), (1, 1, 1))
        self.assertEqual(rows[0]['orch_id'], 'old')
        self.assertFalse(any('token' in key for key in rows[0]))
        self.assertNotIn(self.auth['token'], json.dumps(preview))
        self.assertEqual(list(self.con.iterdump()), before)

    def test_list_counts_suppressed_interim_and_empty_disabled_interface(self):
        rid = self.submit()
        interface.reply(self.con, 'old', request_id=rid, text='working', kind='interim')
        interface.dismiss(self.con, 'old', request_id=rid, reason='skip')
        extra = interface.register(self.con, name='empty', orch_id='old')
        interface.deregister(self.con, name='empty', token=extra['token'])
        rows = interface.list_interfaces(self.con)
        self.assertEqual((rows[0]['pending_interim'], rows[0]['pending_no_reply']), (0, 1))
        self.assertEqual((rows[1]['disabled'], rows[1]['unanswered'], rows[1]['pending_final']), (1, 0, 0))

    def test_invalid_owner_is_atomic_and_stopped_source_can_move(self):
        self.submit()
        before = list(self.con.iterdump())
        for old, new in [('wrong', 'new'), ('old', 'missing'), ('old', 'old'), ('new', 'old')]:
            with self.subTest(old=old, new=new), self.assertRaises(ValueError):
                interface.rebind(self.con, self.rt, old_id=old, new_id=new)
            self.assertEqual(list(self.con.iterdump()), before)
        store.stop_orch(self.con, 'new')
        with self.assertRaisesRegex(ValueError, 'stopped'):
            self.move()
        self.con.execute("UPDATE orchs SET stopped_at=NULL WHERE id='new'")
        store.stop_orch(self.con, 'old')
        self.assertEqual(self.move()['status'], 'done')

    def test_cli_operator_only_and_old_owner_ack_fenced(self):
        rid = self.submit()
        interface.reply(self.con, 'old', request_id=rid, text='answer')
        with mock.patch.dict(os.environ, {'ORCHD_ORCH_ID': 'old'}):
            code, result = self.cli('rebind', '--from', 'old', '--to', 'new')
            self.assertEqual(code, 1)
            self.assertIn('Operator-only', result['message'])
        with mock.patch.dict(os.environ, {'ORCHD_WORKTREE': '/worker'}):
            self.assertEqual(self.cli('rebind', '--from', 'old', '--to', 'new')[0], 1)
        self.assertEqual(self.cli('rebind', '--from', 'old', '--to', 'new')[0], 0)
        with mock.patch.dict(os.environ, {'ORCHD_ORCH_ID': 'old'}):
            self.assertEqual(self.cli('ack', payload=dict(self.auth, conversation='route', request_id=rid, orch_id='new'))[0], 1)
        self.assertEqual(self.cli('ack', payload=dict(self.auth, conversation='route', request_id=rid))[0], 0)

    def test_rebind_does_not_move_tasks_intake_or_desktop_binding(self):
        core.dispatch(self.con, self.rt, orch_thread='old', repo='demo', title='unrelated',
                      instructions='x', done_when='y', model='sonnet', model_reason='r', task_type='code')
        self.con.execute("INSERT INTO entries(id,orch_id,bound_at) VALUES('desktop','old',0)")
        # Snapshot all unrelated tables, including their notification/receipt state.
        def unrelated():
            return [line for line in self.con.iterdump()
                    if '"interfaces"' not in line and '"interface_requests"' not in line]
        before = unrelated()
        self.move()
        self.assertEqual(unrelated(), before)

    def test_notification_failure_reports_committed_and_remains_recoverable(self):
        rid = self.submit()
        with mock.patch.object(interface, 'sweep', side_effect=OSError('private detail')):
            result = self.move()
        self.assertTrue(result['committed'])
        self.assertEqual(result['wake_error'], 'OSError')
        self.assertNotIn('private detail', json.dumps(result))
        self.assertEqual([r['id'] for r in interface.requests(self.con, 'new')], [rid])
        self.assertEqual(interface.sweep(self.con, self.rt), {rid: None})

    def test_concurrent_submit_ack_rebind_keep_identity_and_delivery(self):
        rid = self.submit()
        interface.reply(self.con, 'old', request_id=rid, text='answer')
        barrier = Barrier(4)
        def run(action):
            con = store.connect()
            try:
                barrier.wait(timeout=5)
                if action == 'move':
                    return interface.rebind(con, self.rt, old_id='old', new_id='new')
                if action == 'ack':
                    return interface.ack(con, **self.auth, conversation='route', request_id=rid)
                return self.submit('duplicate', con)
            finally:
                con.close()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(run, ['move', 'ack', 'submit', 'submit']))
        self.assertEqual(results[2], results[3])
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM interface_requests').fetchone()[0], 2)
        self.assertEqual({r[0] for r in self.con.execute('SELECT orch_id FROM interface_requests')}, {'new'})
        self.assertEqual(interface.fetch(self.con, **self.auth, conversation='route'), [])
        self.assertEqual([r['id'] for r in interface.requests(self.con, 'new')], [results[2]])

    def test_rebind_waits_for_inflight_old_wake_then_notifies_new(self):
        with mock.patch.object(interface, 'wake', return_value=None):
            rid = self.submit()
        entered, release, moving = Event(), Event(), Event()
        def notify(con, rt, owner, *args):
            if owner == 'old':
                entered.set()
                self.assertTrue(release.wait(5))
        def run(move):
            con = store.connect()
            try:
                if move:
                    moving.set()
                    return interface.rebind(con, self.rt, old_id='old', new_id='new')
                return interface.wake(con, self.rt, rid)
            finally:
                con.close()
        with mock.patch.object(core, '_notify_orch', side_effect=notify) as transport, ThreadPoolExecutor(max_workers=2) as pool:
            wake = pool.submit(run, False)
            self.assertTrue(entered.wait(5))
            move = pool.submit(run, True)
            self.assertTrue(moving.wait(5))
            self.assertFalse(move.done())
            release.set()
            wake.result(timeout=5)
            move.result(timeout=5)
        self.assertEqual([c.args[2] for c in transport.call_args_list], ['old', 'new'])

    def test_process_crash_rolls_back_or_recovers_committed_move(self):
        rid = self.submit()
        before = list(self.con.iterdump())
        for stage in ('during', 'after'):
            script = '''
import os
from unittest import mock
from orchd import interface, store
from tests.test_orchd import FakeRuntime
con = store.connect()
original = interface.migrate_owner
def crash(*args):
    original(*args)
    os._exit(17)
with mock.patch.object(interface, TARGET, side_effect=EFFECT):
    interface.rebind(con, FakeRuntime(), old_id='old', new_id='new')
'''.replace('TARGET', repr('migrate_owner' if stage == 'during' else 'sweep')).replace('EFFECT', 'crash' if stage == 'during' else 'lambda *args: os._exit(17)')
            result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 17, result.stderr)
            self.con.close()
            self.con = store.connect()
            if stage == 'during':
                self.assertEqual(list(self.con.iterdump()), before)
            else:
                self.assertEqual([r['id'] for r in interface.requests(self.con, 'new')], [rid])
                row = self.con.execute('SELECT * FROM interface_requests').fetchone()
                self.assertIsNone(row['wake_at'])
                with mock.patch.object(core, '_notify_orch') as notify:
                    self.assertEqual(interface.sweep(self.con, self.rt), {rid: None})
                    self.assertEqual(interface.sweep(self.con, self.rt), {})
                self.assertEqual(notify.call_count, 1)
                self.assertEqual(notify.call_args.args[2], 'new')
