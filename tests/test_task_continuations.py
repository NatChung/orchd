"""Synthetic original-request and same-worker continuation tests; no external services."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from orchd import core, interface, store
from tests.test_orchd import FakeRuntime

class ContinuationTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {'ORCHD_CONFIG_FILE': str(self.folder / 'config.toml'), 'ORCHD_HOME': str(self.folder / 'state'), 'ORCHD_CONFIG_DIR': str(self.folder / 'config')})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.con = store.connect(self.folder / 'tasks.db')
        self.addCleanup(self.con.close)
        self.rt = FakeRuntime()
        original_send = self.rt.send_uds
        self.rt.send_uds = lambda path, session, text, **kw: original_send(path, session, text)
        store.register_orch(self.con, 'example-orch', 'claude')
        reg = interface.register(self.con, name='example-adapter', orch_id='example-orch', token='synthetic-token')
        self.request = interface.submit(self.con, self.rt, name='example-adapter', token=reg['token'], conversation='example-chat', request_key='one', text='Scoped example')['request_id']
        self.rt.sent.clear()

    def policy(self):
        (self.folder / 'config.toml').write_text('[continuation]\nrepositories=["demo"]\nrequire_origin=true\nowner="example-review"\n')

    def dispatch(self, model='sonnet', **extra):
        return core.dispatch(self.con, self.rt, orch_thread='example-orch', repo='demo', title='Example', instructions='Scoped work', done_when='Report evidence', model=model, model_reason='fixture', task_type='code', backend='exec', **extra)

    def test_required_origin_fails_before_task_or_worker(self):
        self.policy()
        with self.assertRaisesRegex(ValueError, 'origin_request_id'):
            self.dispatch()
        self.assertEqual(self.con.execute('select count(*) from tasks').fetchone()[0], 0)
        self.assertEqual(self.rt.sent, [])

    def test_origin_dispatch_replay_and_foreign_request(self):
        from orchd import task_origins
        self.policy()
        t = self.dispatch(origin_request_id=self.request, dispatch_key='author')
        old_sent = list(self.rt.sent)
        again = self.dispatch(origin_request_id=self.request, dispatch_key='author')
        self.assertEqual(t['id'], again['id'])
        self.assertEqual(self.rt.sent, old_sent)
        self.assertEqual(task_origins.for_task(self.con, t['id'])['request_id'], self.request)
        with self.assertRaisesRegex(ValueError, 'conflict'):
            core.dispatch(self.con, self.rt, orch_thread='example-orch', repo='demo', title='Changed', instructions='Scoped work', done_when='Report evidence', model='sonnet', model_reason='fixture', task_type='code', backend='exec', origin_request_id=self.request, dispatch_key='author')
        self.con.execute("UPDATE interface_requests SET orch_id='other' WHERE id=?", (self.request,))
        with self.assertRaisesRegex(ValueError, 'owned'):
            self.dispatch(origin_request_id=self.request, dispatch_key='other')

    def test_close_defers_original_worker_then_release_closes_once(self):
        from orchd import task_continuations as leases
        self.policy()
        t = self.dispatch(origin_request_id=self.request, dispatch_key='author')
        core.report(self.con, self.rt, t['id'], 'done', 'original result', 'evidence')
        lease = leases.bind_original_worker(self.con, t['id'])
        close = core.close(self.con, self.rt, t['id'], outcome='done')
        self.assertFalse(close['closed'])
        self.assertEqual(close['deferred'], 'continuation')
        self.assertEqual(self.rt.stopped, [])
        self.assertEqual(self.rt.removed, [])
        self.assertEqual(core.close(self.con, self.rt, t['id'], outcome='done'), close)
        with self.assertRaisesRegex(ValueError, 'conflict'):
            core.close(self.con, self.rt, t['id'], outcome='parked')
        initial = self.con.execute("SELECT * FROM native_provider_deliveries WHERE provider='claude-initial'").fetchone()
        events = [dict(type='user', uuid=initial['id'], sessionId=t['session_id'], message=dict(content=initial['wrapped_input'])), dict(type='assistant', uuid='initial-final', parentUuid=initial['id'], message=dict(stop_reason='end_turn', content=[dict(type='text', text='Original author finished')]))]
        with self.write_transcript(t, events):
            result = leases.release(self.con, self.rt, lease['id'], lease['worker_digest'], reason='pr_closed')
        self.assertTrue(result['released'])
        self.assertEqual(store.get_task(self.con, t['id'])['status'], 'closed')
        stopped = list(self.rt.stopped)
        self.assertTrue(leases.release(self.con, self.rt, lease['id'], lease['worker_digest'], reason='pr_closed')['released'])
        self.assertEqual(self.rt.stopped, stopped)

    def test_followup_key_reuses_acceptance_and_original_session(self):
        t = self.dispatch()
        self.rt.sent.clear()
        self.rt.send_uds = lambda path, session, text, **kw: self.rt.sent.append((path, session, text))
        expected = {k: t[k] for k in store.ANSWER_TARGET_FIELDS}
        a = core.followup(self.con, self.rt, t['id'], 'public probe', expected=expected, idempotency_key='cycle-one')
        b = core.followup(self.con, self.rt, t['id'], 'public probe', expected=expected, idempotency_key='cycle-one')
        self.assertEqual(a['acceptance_id'], b['acceptance_id'])
        self.assertEqual(len(self.rt.sent), 1)
        with self.assertRaisesRegex(ValueError, 'conflict'):
            core.followup(self.con, self.rt, t['id'], 'different', expected=expected, idempotency_key='cycle-one')

    def managed_followup(self):
        self.rt.send_uds = lambda path, session, text, **kw: self.rt.sent.append((path, session, text, kw.get('message_uuid')))
        task = self.dispatch()
        self.rt.sent.clear()
        target = {k: task[k] for k in store.ANSWER_TARGET_FIELDS}
        receipt = core.followup(self.con, self.rt, task['id'], 'public probe', expected=target, idempotency_key='cycle-one')
        return (task, target, receipt)

    def write_transcript(self, task, events):
        root = self.folder / 'transcripts'
        location = root / 'example-project'
        location.mkdir(parents=True, exist_ok=True)
        (location / (task['session_id'] + '.jsonl')).write_text(''.join((json.dumps(e) + '\n' for e in events)))
        return patch.dict(os.environ, {'ORCHD_CLAUDE_PROJECTS': str(root)})

    def test_claude_actual_frame_parent_chain_and_not_later_answer(self):
        from orchd import task_continuations as leases
        task, target, receipt = self.managed_followup()
        row = self.con.execute("select * from native_provider_deliveries where provider='claude'").fetchone()
        events = [dict(type='user', uuid=row['id'], sessionId=task['session_id'], message=dict(content=row['wrapped_input'])), dict(type='assistant', uuid='final-one', parentUuid=row['id'], message=dict(stop_reason='end_turn', content=[dict(type='text', text='Actual public final')])), dict(type='user', uuid='later-user', sessionId=task['session_id'], parentUuid='final-one', message=dict(content='Unrelated later request')), dict(type='assistant', uuid='later-final', parentUuid='later-user', message=dict(stop_reason='end_turn', content=[dict(type='text', text='Not the original result')]))]
        with self.write_transcript(task, events):
            out = leases.followup_evidence(self.con, self.rt, task['id'], receipt['acceptance_id'], expected=target)
        self.assertEqual((out['status'], out['final_text'], out['turn_id']), ('completed', 'Actual public final', 'final-one'))
        self.assertEqual(out['delivery']['followup_ids'], [receipt['acceptance_id']])
        self.assertEqual(out['delivery']['journal_state'], 'submitted')
        self.assertEqual(out['delivery']['state'], 'completed')
        self.assertEqual(len(self.rt.sent), 1)

    def test_claude_unknown_write_recovers_only_from_actual_admission(self):
        from orchd import task_continuations as leases
        from orchd.runtime import UDSWriteUncertain
        task = self.dispatch()
        target = {k: task[k] for k in store.ANSWER_TARGET_FIELDS}
        self.rt.sent.clear()
        calls = []

        def uncertain(*args, **kwargs):
            calls.append(kwargs['message_uuid'])
            raise UDSWriteUncertain('synthetic interrupted write')
        self.rt.send_uds = uncertain
        receipt = core.followup(self.con, self.rt, task['id'], 'public probe', expected=target, idempotency_key='cycle-one')
        again = core.followup(self.con, self.rt, task['id'], 'public probe', expected=target, idempotency_key='cycle-one')
        self.assertEqual(len(calls), 1)
        self.assertTrue(again['uncertain'])
        row = self.con.execute("select * from native_provider_deliveries where provider='claude'").fetchone()
        events = [dict(type='queued_command', uuid=row['id'], sessionId=task['session_id'])]
        with self.write_transcript(task, events):
            self.assertNotEqual(leases.followup_evidence(self.con, self.rt, task['id'], receipt['acceptance_id'], expected=target)['status'], 'completed')
        events = [dict(type='user', uuid=row['id'], sessionId=task['session_id'], message=dict(content=row['wrapped_input'])), dict(type='assistant', uuid='actual-final', parentUuid=row['id'], message=dict(stop_reason='end_turn', content=[dict(type='text', text='Recovered final')]))]
        with self.write_transcript(task, events):
            out = leases.followup_evidence(self.con, self.rt, task['id'], receipt['acceptance_id'], expected=target)
        self.assertEqual(out['status'], 'completed')
        self.assertEqual(out['delivery']['journal_state'], 'unknown')
        self.assertEqual(self.con.execute("select state from native_provider_deliveries where provider='claude'").fetchone()[0], 'unknown')

    def test_wrong_claude_input_or_multiple_terminal_answers_refused(self):
        from orchd import task_continuations as leases
        task, target, receipt = self.managed_followup()
        row = self.con.execute("select * from native_provider_deliveries where provider='claude'").fetchone()
        user = dict(type='user', uuid=row['id'], sessionId=task['session_id'], message=dict(content='Changed input'))
        with self.write_transcript(task, [user]):
            with self.assertRaisesRegex(ValueError, 'content changed'):
                leases.followup_evidence(self.con, self.rt, task['id'], receipt['acceptance_id'], expected=target)
        user['message']['content'] = row['wrapped_input']
        answers = [dict(type='assistant', uuid='f' + str(i), parentUuid=row['id'], message=dict(stop_reason='end_turn', content=[dict(type='text', text='answer')])) for i in range(2)]
        with self.write_transcript(task, [user, *answers]):
            with self.assertRaisesRegex(ValueError, 'ambiguous'):
                leases.followup_evidence(self.con, self.rt, task['id'], receipt['acceptance_id'], expected=target)

    def test_codex_native_exact_wrapped_input_turn_and_readonly_client_id_recovery(self):
        from orchd import task_continuations as leases, app_worker
        task = self.dispatch(model='sol')
        store.update_task(self.con, task['id'], backend='app-server', generation='original-generation', endpoint='/synthetic/provider.sock')
        task = store.get_task(self.con, task['id'])
        target = {k: task[k] for k in store.ANSWER_TARGET_FIELDS}
        with patch.object(app_worker, 'control', return_value=dict(status='queued', delivered=0, pending=1)):
            receipt = core.followup(self.con, self.rt, task['id'], 'public probe', expected=target, idempotency_key='cycle-one')
        queued = store.pending_answers(self.con, task['id'])
        wrapped = '\n\n'.join((f"[orchd answer {task['id']}]\n{r['body']}" for r in queued))
        self.con.execute('INSERT INTO worker_deliveries VALUES(?,?,?,?,?,?,?,?,?)', ('delivery-one', task['id'], task['generation'], task['session_id'], None, json.dumps([r['id'] for r in queued]), 'uncertain', None, 1.0))
        turn = dict(id='actual-turn', status='completed', items=[dict(type='userMessage', clientId='delivery-one', content=[dict(type='text', text=wrapped)]), dict(type='agentMessage', phase='final_answer', text='Bound public final')])
        self.con.execute('INSERT INTO worker_deliveries VALUES(?,?,?,?,?,?,?,?,?)', ('rejected-before', task['id'], task['generation'], task['session_id'], None, json.dumps([r['id'] for r in queued]), 'rejected', 'provider refused before turn', 0.5))
        calls = []

        class ReadOnlyRPC:

            def __init__(self, endpoint):
                pass

            def call(self, method, params):
                calls.append(method)
                return dict(data=[turn], nextCursor=None)

            def close(self):
                pass
        with patch('orchd.app_transport.RPC', ReadOnlyRPC):
            out = leases.followup_evidence(self.con, self.rt, task['id'], receipt['acceptance_id'], expected=target)
        self.assertEqual(out['final_text'], 'Bound public final')
        self.assertEqual(out['delivery']['turn_id'], 'actual-turn')
        self.assertEqual(out['delivery']['journal_state'], 'uncertain')
        self.assertEqual(calls, ['thread/turns/list'])
        turn['status'] = 'failed'
        with patch('orchd.app_transport.RPC', ReadOnlyRPC):
            failed = leases.followup_evidence(self.con, self.rt, task['id'], receipt['acceptance_id'], expected=target)
        self.assertEqual((failed['status'], failed['error_code']), ('failed', 'provider_turn_failed'))
        turn['status'] = 'completed'
        turn['items'][0]['content'][0]['text'] = 'Wrong original input'
        with patch('orchd.app_transport.RPC', ReadOnlyRPC):
            with self.assertRaisesRegex(ValueError, 'differs'):
                leases.followup_evidence(self.con, self.rt, task['id'], receipt['acceptance_id'], expected=target)

    def test_lookup_only_missing_key_cannot_queue_and_original_lease_precedes_spawn(self):
        from orchd import task_continuations as leases
        self.policy()
        create = self.rt.create_worktree
        seen = []

        def checked(repo_path, repo, task_id):
            seen.append(leases.get(self.con, task_id)['state'])
            return create(repo_path, repo, task_id)
        self.rt.create_worktree = checked
        task = self.dispatch(origin_request_id=self.request, dispatch_key='author')
        self.rt.sent.clear()
        self.assertEqual(seen, ['held'])
        self.assertIsNone(leases.followup_for_key(self.con, task['id'], 'never-accepted', instruction='test'))
        self.assertEqual(self.rt.sent, [])
        self.assertEqual(store.pending_answer_count(self.con, task['id']), 0)

    def test_lease_release_refuses_changed_original_worker(self):
        from orchd import task_continuations as leases
        self.policy()
        task = self.dispatch(origin_request_id=self.request, dispatch_key='author')
        lease = leases.get(self.con, task['id'])
        store.update_task(self.con, task['id'], session_id='replacement-session')
        with self.assertRaisesRegex(ValueError, 'identity changed'):
            leases.release(self.con, self.rt, lease['id'], lease['worker_digest'])
        self.assertEqual(self.rt.stopped, [])
        self.assertEqual(self.rt.removed, [])

    def test_origin_failure_incident_dedup_and_same_intent_recovers(self):
        from orchd import task_origins
        self.policy()
        for _ in range(2):
            with self.assertRaisesRegex(ValueError, 'origin_request_id'):
                self.dispatch()
        incidents = task_origins.list_dispatch_incidents(self.con)
        self.assertEqual(len(incidents), 1)
        self.assertEqual(incidents[0]['code'], 'origin_missing')
        self.assertEqual(incidents[0]['state'], 'blocked')
        self.assertNotIn('instructions', incidents[0])
        self.assertNotIn('body', incidents[0])
        task = self.dispatch(origin_request_id=self.request, dispatch_key='author')
        resolved = task_origins.list_dispatch_incidents(self.con)[0]
        self.assertEqual((resolved['id'], resolved['state'], resolved['task_id']), (incidents[0]['id'], 'resolved', task['id']))

    def test_held_lease_prevents_explicit_or_automatic_provider_replacement(self):
        self.policy()
        task = self.dispatch(model='sol', origin_request_id=self.request, dispatch_key='author')
        store.update_task(self.con, task['id'], auto_model=1)
        self.rt.stopped.clear()
        self.rt.resumed.clear()
        with self.assertRaisesRegex(ValueError, 'forbids replacement'):
            core.retry(self.con, self.rt, task['id'], 'sonnet', 'provider exhausted')
        requested = []
        self.rt.spawn_auto_retry = lambda *args: requested.append(args)
        core.rate_limit_notice(self.con, self.rt, task['id'], {'message': 'You’ve hit your usage limit. Please try again later.'}, 'first-limit')
        self.assertEqual(requested, [])
        self.assertIsNone(self.con.execute("select 1 from messages where task_id=? and kind='auto_retry_requested'", (task['id'],)).fetchone())
        self.assertEqual(self.rt.stopped, [])
        self.assertEqual(self.rt.resumed, [])
        self.assertEqual(store.get_task(self.con, task['id'])['session_id'], task['session_id'])

    def test_dismissed_origin_keeps_retained_worker_until_explicit_task_abandon(self):
        from orchd import task_continuations as leases
        self.policy()
        task = self.dispatch(origin_request_id=self.request, dispatch_key='author')
        self.con.execute('update interface_requests set dismissed_at=1 where id=?', (self.request,))
        deferred = core.close(self.con, self.rt, task['id'], outcome='done')
        self.assertFalse(deferred['closed'])
        self.assertEqual(self.rt.stopped, [])
        self.assertEqual(leases.get(self.con, task['id'])['state'], 'held')
        result = core.close(self.con, self.rt, task['id'], outcome='abandoned')
        self.assertTrue(result['closed'])
        self.assertEqual(leases.get(self.con, task['id'])['state'], 'released')
        self.assertTrue(self.rt.stopped)

    def test_release_holds_active_queued_and_unresolved_delivery_under_native_owner(self):
        from orchd import task_continuations as leases
        self.policy()
        task = self.dispatch(origin_request_id=self.request, dispatch_key='author')
        core.report(self.con, self.rt, task['id'], 'done', 'original result', 'evidence')
        lease = leases.get(self.con, task['id'])
        core.close(self.con, self.rt, task['id'], outcome='done')
        store.update_task(self.con, task['id'], active_turn='busy-original-turn', turn_state='active')
        with self.assertRaisesRegex(RuntimeError, 'active'):
            leases.release(self.con, self.rt, lease['id'], lease['worker_digest'])
        self.assertEqual(self.rt.stopped, [])
        store.update_task(self.con, task['id'], active_turn=None, turn_state=None)
        store.add_message(self.con, task['id'], store.QUEUED, 'real pending input')
        with self.assertRaisesRegex(RuntimeError, 'queued'):
            leases.release(self.con, self.rt, lease['id'], lease['worker_digest'])
        self.assertEqual(self.rt.removed, [])
        store.mark_read(self.con, [r['id'] for r in store.pending_answers(self.con, task['id'])])
        self.con.execute("UPDATE native_provider_deliveries SET state='unknown' WHERE task_id=?", (task['id'],))
        with self.assertRaisesRegex(RuntimeError, 'reconciliation'):
            leases.release(self.con, self.rt, lease['id'], lease['worker_digest'])
        self.assertEqual(leases.get(self.con, task['id'])['state'], 'held')
        self.assertEqual(self.rt.stopped, [])

    def test_initial_uncertain_frame_keeps_original_worktree_and_dispatch_replay_never_spawns(self):
        from orchd.runtime import UDSWriteUncertain
        from orchd import task_continuations as leases
        self.policy()
        calls = []

        def uncertain(path, session, text, **kw):
            calls.append(kw['message_uuid'])
            raise UDSWriteUncertain('synthetic initial socket uncertainty')
        self.rt.send_uds = uncertain
        with self.assertRaises(UDSWriteUncertain):
            self.dispatch(origin_request_id=self.request, dispatch_key='author')
        task = dict(self.con.execute('SELECT * FROM tasks').fetchone())
        lease = leases.get(self.con, task['id'])
        self.assertEqual(task['status'], 'failed')
        self.assertEqual(lease['worker']['session_id'], task['session_id'])
        self.assertEqual(self.rt.removed, [])
        delivery = self.con.execute("SELECT * FROM native_provider_deliveries WHERE provider='claude-initial'").fetchone()
        self.assertEqual((delivery['id'], delivery['state']), (calls[0], 'unknown'))
        again = self.dispatch(origin_request_id=self.request, dispatch_key='author')
        self.assertEqual(again['id'], task['id'])
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM tasks').fetchone()[0], 1)
        with self.assertRaisesRegex(RuntimeError, 'positive evidence'):
            leases.release(self.con, self.rt, lease['id'], lease['worker_digest'])
        self.assertEqual(self.rt.stopped, [])

    def test_group_intent_conflict_and_replayed_managed_batch_keep_original_tasks(self):
        self.policy()
        task = self.dispatch(origin_request_id=self.request, dispatch_key='author', group_id='original-group')
        with self.assertRaisesRegex(ValueError, 'conflict'):
            self.dispatch(origin_request_id=self.request, dispatch_key='author', group_id='different-group')
        options = dict(repo='demo', title='Independent batch author', instructions='Scoped batch work', done_when='Report', model='sonnet', model_reason='fixture', task_type='code', backend='exec', origin_request_id=self.request, dispatch_key='batch-author')
        first = core.dispatch_batch(self.con, self.rt, orch_thread='example-orch', tasks=[options])
        second = core.dispatch_batch(self.con, self.rt, orch_thread='example-orch', tasks=[options])
        self.assertEqual(first['group_id'], second['group_id'])
        self.assertEqual(first['tasks'][0]['task_id'], second['tasks'][0]['task_id'])
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM tasks').fetchone()[0], 2)

    def test_submitted_claude_frame_does_not_mean_execution_complete_for_release(self):
        from orchd import task_continuations as leases
        self.policy()
        task = self.dispatch(origin_request_id=self.request, dispatch_key='author')
        core.report(self.con, self.rt, task['id'], 'done', 'original result', 'evidence')
        lease = leases.get(self.con, task['id'])
        core.close(self.con, self.rt, task['id'], outcome='done')
        initial = self.con.execute("SELECT * FROM native_provider_deliveries WHERE provider='claude-initial'").fetchone()
        events = [dict(type='user', uuid=initial['id'], sessionId=task['session_id'], message=dict(content=initial['wrapped_input'])), dict(type='assistant', uuid='initial-final', parentUuid=initial['id'], message=dict(stop_reason='end_turn', content=[dict(type='text', text='Initial turn done')]))]
        expected = {k: task[k] for k in store.ANSWER_TARGET_FIELDS}
        receipt = core.followup(self.con, self.rt, task['id'], 'second original turn', expected=expected, idempotency_key='running-claude')
        with self.write_transcript(task, events):
            with self.assertRaisesRegex(RuntimeError, 'positive evidence'):
                leases.release(self.con, self.rt, lease['id'], lease['worker_digest'])
        self.assertEqual(self.rt.stopped, [])
        self.assertEqual(leases.get(self.con, task['id'])['state'], 'held')
        frame = self.con.execute("SELECT * FROM native_provider_deliveries WHERE provider='claude'").fetchone()
        events += [dict(type='user', uuid=frame['id'], sessionId=task['session_id'], parentUuid='initial-final', message=dict(content=frame['wrapped_input'])), dict(type='assistant', uuid='followup-final', parentUuid=frame['id'], message=dict(stop_reason='end_turn', content=[dict(type='text', text='Second turn done')]))]
        with self.write_transcript(task, events):
            self.assertTrue(leases.release(self.con, self.rt, lease['id'], lease['worker_digest'])['released'])
        self.assertEqual(store.get_task(self.con, task['id'])['status'], 'closed')
    def test_failed_acceptance_receipt_can_be_recovered_readonly_without_new_send(self):
        from orchd import task_continuations as leases
        task = self.dispatch()
        target = {k:task[k] for k in store.ANSWER_TARGET_FIELDS}
        calls = []
        def lost_reply(path, session, text, **kw):
            calls.append(kw['message_uuid'])
            raise RuntimeError('synthetic exception after original frame acceptance')
        self.rt.send_uds = lost_reply
        with self.assertRaises(RuntimeError):
            core.followup(self.con, self.rt, task['id'], 'original public request', expected=target, idempotency_key='once')
        receipt = leases.followup_for_key(self.con, task['id'], 'once', expected=target, instruction='original public request')
        self.assertEqual(receipt['status'], 'queued')
        self.assertEqual(receipt['native_status'], 'failed')
        self.assertTrue(receipt['uncertain'])
        row = self.con.execute("SELECT * FROM native_provider_deliveries WHERE provider='claude'").fetchone()
        events = [dict(type='user', uuid=row['id'], sessionId=task['session_id'], message=dict(content=row['wrapped_input'])), dict(type='assistant', uuid='actual-completed', parentUuid=row['id'], message=dict(stop_reason='end_turn', content=[dict(type='text', text='Original request completed')]))]
        with self.write_transcript(task, events):
            proof = leases.followup_evidence(self.con, self.rt, task['id'], receipt['acceptance_id'], expected=target)
        self.assertEqual(proof['status'], 'completed')
        self.assertEqual(proof['final_text'], 'Original request completed')
        self.assertEqual(len(calls), 1)

    def test_ordinary_leased_answer_is_also_tracked_before_release(self):
        from orchd import task_continuations as leases
        self.policy()
        task = self.dispatch(origin_request_id=self.request, dispatch_key='author')
        core.report(self.con, self.rt, task['id'], 'done', 'original result', 'evidence')
        lease = leases.get(self.con, task['id'])
        core.close(self.con, self.rt, task['id'], outcome='done')
        core.answer(self.con, self.rt, task['id'], 'ordinary original answer')
        ordinary = self.con.execute("SELECT * FROM native_provider_deliveries WHERE provider='claude'").fetchone()
        self.assertEqual(json.loads(ordinary['followup_ids']), [])
        self.assertTrue(json.loads(ordinary['message_ids']))
        with self.assertRaisesRegex(RuntimeError, 'positive evidence'):
            leases.release(self.con, self.rt, lease['id'], lease['worker_digest'])
        self.assertEqual(self.rt.stopped, [])

    def test_positive_reconciliation_completes_original_pending_receipt_once(self):
        from orchd import task_continuations as leases
        task, target, receipt = self.managed_followup()
        row = self.con.execute("SELECT * FROM native_provider_deliveries WHERE provider='claude'").fetchone()
        self.con.execute("DELETE FROM messages WHERE task_id=? AND kind='answer'", (task['id'],))
        self.con.execute("UPDATE native_provider_deliveries SET state='unknown' WHERE id=?", (row['id'],))
        events = [dict(type='user', uuid=row['id'], sessionId=task['session_id'], message=dict(content=row['wrapped_input'])), dict(type='assistant', uuid='bound-final', parentUuid=row['id'], message=dict(stop_reason='end_turn', content=[dict(type='text', text='Completed original operation')]))]
        before = len(self.rt.sent)
        with self.write_transcript(task, events):
            first = leases.reconcile_followup(self.con, self.rt, task['id'], receipt['acceptance_id'], expected=target)
            second = leases.reconcile_followup(self.con, self.rt, task['id'], receipt['acceptance_id'], expected=target)
        self.assertEqual(first, second)
        self.assertEqual(len(self.rt.sent), before)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM messages WHERE task_id=? AND kind='answer'", (task['id'],)).fetchone()[0], 1)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM messages WHERE task_id=? AND kind='provider_reconciled'", (task['id'],)).fetchone()[0], 1)

    def test_generic_initial_and_ordinary_receipts_reconcile_without_sending(self):
        from orchd import task_continuations as leases
        self.policy()
        task = self.dispatch(origin_request_id=self.request, dispatch_key='author')
        target = {key: task[key] for key in store.ANSWER_TARGET_FIELDS}
        core.report(self.con, self.rt, task['id'], 'done', 'original result', 'evidence')
        core.answer(self.con, self.rt, task['id'], 'ordinary original answer')
        rows = self.con.execute('SELECT * FROM native_provider_deliveries WHERE task_id=?', (task['id'],)).fetchall()
        self.assertEqual(len(rows), 2)
        events = []
        for index, row in enumerate(rows):
            events.extend([dict(type='user', uuid=row['id'], sessionId=task['session_id'], message=dict(content=row['wrapped_input'])), dict(type='assistant', uuid=f'public-final-{index}', parentUuid=row['id'], message=dict(stop_reason='end_turn', content=[dict(type='text', text='Original input completed')]))])
        before = len(self.rt.sent)
        with self.write_transcript(task, events):
            for row in rows:
                proof = leases.provider_evidence(self.con, self.rt, task['id'], row['id'], expected=target)
                self.assertEqual(proof['status'], 'completed')
                first = leases.reconcile_provider(self.con, self.rt, task['id'], row['id'], expected=target)
                self.assertEqual(first, leases.reconcile_provider(self.con, self.rt, task['id'], row['id'], expected=target))
        self.assertEqual(len(self.rt.sent), before)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM messages WHERE task_id=? AND kind='provider_reconciled'", (task['id'],)).fetchone()[0], 2)

    def test_native_first_session_receipt_freezes_partial_generation_before_agent_input(self):
        from orchd import task_continuations as leases, app_worker
        self.policy()
        def started(con, runtime, task, prompt, model, **kwargs):
            store.update_task(con, task['id'], generation='original-generation', session_id=None, endpoint='/synthetic/provider.sock')
        with patch.object(app_worker, 'start', started):
            task = core.dispatch(self.con, self.rt, orch_thread='example-orch', repo='demo', title='Native original', instructions='Scoped work', done_when='Report', model='sol', model_reason='fixture', task_type='code', backend='app-server', origin_request_id=self.request, dispatch_key='native-author')
        self.assertIsNone(leases.get(self.con, task['id'])['worker'])
        store.update_task(self.con, task['id'], session_id='actual-native-session')
        lease = leases.seed_worker(self.con, task['id'])
        self.assertEqual(lease['worker']['session_id'], 'actual-native-session')
        store.update_task(self.con, task['id'], session_id='different-session')
        with self.assertRaisesRegex(ValueError, 'identity changed'):
            leases.bind_original_worker(self.con, task['id'])

if __name__ == '__main__':
    unittest.main()
