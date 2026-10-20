import json
import datetime
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from orchd import app_worker, core, mcp_server, quota, routing, store
from tests.test_retry import RetryRuntime


class RoutingRuntime(RetryRuntime):
    def __init__(self):
        super().__init__()
        self.auto_requests = []

    def start_auto_retry(self, database, task_id, event_id):
        self.auto_requests.append((task_id, event_id))


class RoutingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {'ORCHD_HOME': self.tmp.name, 'ORCHD_CLAUDE': str(Path(self.tmp.name) / 'no-claude')})
        env.start()
        self.addCleanup(env.stop)
        self.con = store.connect()
        self.addCleanup(self.con.close)
        self.rt = RoutingRuntime()
        self.args = dict(repo='demo', title='Synthetic', instructions='Synthetic', done_when='Synthetic',
                         model='auto', backend='exec', model_reason='independent', task_type='code')

    def test_provider_error_formats_and_contextual_fallback(self):
        # Source: openai/codex tag rust-v0.160.1:
        # codex-rs/app-server-protocol/schema/typescript/v2/{TurnError,CodexErrorInfo}.ts
        # codex-rs/exec/src/exec_events.rs: TurnFailed + ThreadErrorEvent
        # codex-rs/protocol/src/error.rs: UsageLimitReachedError::fmt
        # These are source-shaped fixtures, not captured live quota failures.
        for error in (
            {'message': 'Limit reached', 'codexErrorInfo': 'usageLimitExceeded', 'additionalDetails': None},
            {'message': 'Limit reached', 'codexErrorInfo': 'rateLimitExceeded'},
            {'message': 'Connection failed', 'codexErrorInfo': {
                'httpConnectionFailed': {'httpStatusCode': 429}}},
            {'code': 'usage_limit_exceeded'}, {'type': 'rate_limit'},
            {'status_code': 429}, {'httpStatusCode': 429},
            {'code': -32000, 'message': 'Rejected', 'data': {'type': 'rate_limit'}},
            {'message': 'You’ve hit your usage limit. Please try again later.'},
            "You've hit your usage limit. Please try again later.",
            '429 Too Many Requests', 'status 429',
            'unexpected status code: 429 Too Many Requests',
            'rate limit exceeded: upstream request limit',
        ):
            with self.subTest(error=error):
                self.assertTrue(routing.limited(error))
        for error in (
            'parse error at line 429', 'gh: API rate limit exceeded for user',
            'test log: 429', 'curl output: HTTP/1.1 429 Too Many Requests',
            '429', 'rate limit', 'usage limit exceeded',
            {'message': 'parse error at line 429'},
            {'message': 'gh: API rate limit exceeded for user'},
            {'message': '429 Too Many Requests', 'codexErrorInfo': 'sandboxError'},
            {'output': 'status 429'},
            {'data': {'output': '429 Too Many Requests'}},
            {'type': 'item.completed', 'item': {'type': 'command_execution',
                'aggregated_output': 'HTTP/1.1 429 Too Many Requests'}},
        ):
            with self.subTest(error=error):
                self.assertFalse(routing.limited(error))

    def test_exec_tool_output_and_unrelated_failed_turns_do_not_limit(self):
        task = self.dispatch()
        events = [{'type': 'item.completed', 'item': {'id': 'cmd',
            'type': 'command_execution', 'command': command, 'status': 'failed',
            'exit_code': 1, 'aggregated_output': output}}
            for command, output in (
                ('parser', 'parse error at line 429'),
                ('gh api user', 'gh: API rate limit exceeded for user'),
                ('pytest', 'test log: 429'),
                ('curl https://example.com', 'HTTP/1.1 429 Too Many Requests'))]
        # Even provider turn failures need provider-specific error context.
        events += [{'type': 'turn.failed', 'error': {'message': text}}
                   for text in ('parse error at line 429', 'gh: API rate limit exceeded for user')]
        log = Path(self.tmp.name) / 'tools.jsonl'
        log.write_text(''.join(json.dumps(event) + '\n' for event in events))
        core.auto_flush(self.con, self.rt, task['id'], int(task['job_id']), turn_log=log)
        self.assertEqual(self.rt.auto_requests, [])
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM messages WHERE kind='rate_limit'").fetchone()[0], 0)
        self.assertEqual(routing.select(self.con)['selected_alias'], 'sol')

    def test_app_command_output_is_not_a_provider_error(self):
        task = self.dispatch()
        store.update_task(self.con, task['id'], backend='app-server', generation='g', active_turn='t')
        supervisor = app_worker.Supervisor(self.con.execute('PRAGMA database_list').fetchone()[2], task['id'], 'g')
        self.addCleanup(supervisor.con.close)
        with patch('orchd.runtime.Runtime', return_value=self.rt):
            supervisor.event({'method': 'item/completed', 'params': {
                'threadId': task['session_id'], 'turnId': 't', 'item': {
                    'id': 'cmd', 'type': 'commandExecution', 'status': 'failed',
                    'aggregatedOutput': 'HTTP/1.1 429 Too Many Requests'}}})
            supervisor.event({'method': 'turn/completed', 'params': {
                'threadId': task['session_id'], 'turn': {'id': 't', 'status': 'failed',
                    'error': {'message': 'gh: API rate limit exceeded for user',
                              'codexErrorInfo': 'sandboxError'}}}})
        self.assertEqual(self.rt.auto_requests, [])
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM messages WHERE kind='rate_limit'").fetchone()[0], 0)
        self.assertEqual(routing.select(self.con)['selected_alias'], 'sol')

    def observe(self, sol, sonnet=20, age=0):
        now = time.time()
        cache = quota.Cache()
        cache.codex({'rateLimits': {'primary': {'usedPercent': sol,
            'windowDurationMins': 300, 'resetsAt': now + 10000}}}, now=now-age)
        cache.claude({'rate_limits': {name: {'used_percentage': sonnet, 'resets_at': now+10000}
                                      for name in ('five_hour', 'seven_day')}}, now=now-age)

    def dispatch(self, **options):
        return core.dispatch(self.con, self.rt, orch_thread='orch', **dict(self.args, **options))

    def test_full_sol_switches_and_metadata_visible(self):
        self.observe(95)
        task = self.dispatch()
        self.assertEqual(task['model'], core.WORKER_MODELS['sonnet'])
        self.assertIn('remaining capacity/reset-time score', task['model_reason'])
        msg = self.con.execute("SELECT body FROM messages WHERE task_id=? AND kind='dispatch'", (task['id'],)).fetchone()
        applied = json.loads(msg['body'])['quota_shadow']
        self.assertTrue(applied['applied'])
        self.assertEqual(applied['policy_version'], 'quota-shadow-v1')
        self.assertEqual(applied['selected_alias'], 'sonnet')
        self.assertEqual(core.list_open(self.con, self.rt)[0]['model_reason'], task['model_reason'])
        core.progress(self.con, self.rt, task['id'], 'Synthetic')
        self.assertEqual(core.inbox(self.con, 'orch')[0]['model_reason'], task['model_reason'])

    def test_unknown_and_stale_fallback_record_reason(self):
        for age in (None, quota.TTL + 1):
            if age is not None:
                self.observe(99, age=age)
            task = self.dispatch()
            self.assertEqual(task['model'], core.WORKER_MODELS['sol'])
            self.assertIn('quota unknown/stale; fallback', task['model_reason'])

    def test_explicit_and_omitted_models_preserved(self):
        self.observe(100)
        for model in ('sol', 'sonnet'):
            task = self.dispatch(model=model)
            self.assertEqual(task['model'], core.WORKER_MODELS[model])
            self.assertEqual(task['model_reason'], 'independent')
        with patch.dict(os.environ, {'ORCHD_WORKER_MODEL': 'sonnet'}):
            self.assertEqual(self.dispatch(model=None)['model'], core.WORKER_MODELS['sonnet'])

    def test_both_full_choose_remaining_known_quota(self):
        self.observe(99, 96)
        self.assertEqual(self.dispatch()['model'], core.WORKER_MODELS['sonnet'])
        self.observe(95, 100)
        self.assertEqual(self.dispatch()['model'], core.WORKER_MODELS['sol'])

    def test_explicit_app_backend_remains_sol(self):
        self.observe(100)
        self.assertEqual(routing.select(self.con, backend='app-server')['selected_alias'], 'sol')

    def test_exec_failure_notifies_once_and_retry_switches_same_worktree(self):
        task = self.dispatch()
        log = Path(self.tmp.name) / 'turn.jsonl'
        log.write_text(json.dumps({'type': 'turn.failed', 'error': {'message': "You’ve hit your usage limit. Please try again later."}})+'\n')
        for _ in range(2):
            core.auto_flush(self.con, self.rt, task['id'], int(task['job_id']), turn_log=log)
        self.assertEqual(len(self.rt.woken), 1)
        self.assertIn('automatic sonnet retry scheduled', core.inbox(self.con, 'orch')[0]['body'])
        self.assertEqual(len(self.rt.auto_requests), 1)
        self.assertEqual(self.dispatch()['model'], core.WORKER_MODELS['sonnet'])
        result = core.auto_retry(self.con, self.rt, *self.rt.auto_requests[0])
        self.assertEqual(result['status'], 'running')
        switched = store.get_task(self.con, task['id'])
        self.assertEqual(switched['model'], core.WORKER_MODELS['sonnet'])
        self.assertEqual(switched['worktree'], task['worktree'])
        self.assertEqual(switched['auto_retry_count'], 1)
        self.assertIn('Continue from the existing worktree', self.rt.sent[-1][2])
        self.assertIn('Do not redo', self.rt.sent[-1][2])
        self.assertIn('actions already sent', self.rt.sent[-1][2])
        self.assertEqual(core.auto_retry(self.con, self.rt, *self.rt.auto_requests[0])['status'], 'skipped')
        core.retry(self.con, self.rt, task['id'], 'sol', 'manual test', backend='exec')
        core.rate_limit_notice(self.con, self.rt, task['id'], {'code': 'usage_limit_exceeded'}, 'second')
        self.assertEqual(len(self.rt.auto_requests), 1)
        self.assertFalse(core.rate_limit_notice(self.con, self.rt, task['id'], {'code': 'usage_limit_exceeded'}, 'second'))

    def test_app_turn_failure_notifies_and_ignores_old_completion(self):
        task = self.dispatch()
        store.update_task(self.con, task['id'], backend='app-server', generation='g', active_turn='t')
        supervisor = app_worker.Supervisor(self.con.execute('PRAGMA database_list').fetchone()[2], task['id'], 'g')
        self.addCleanup(supervisor.con.close)
        event = {'method': 'turn/completed', 'params': {'threadId': task['session_id'],
            'turn': {'id': 't', 'status': 'failed', 'error': {'message': 'Limit reached', 'codexErrorInfo': 'usageLimitExceeded'}}}}
        with patch('orchd.runtime.Runtime', return_value=self.rt):
            supervisor.event(event)
            supervisor.event(event)
        self.assertEqual(len(self.rt.woken), 1)
        self.assertIsNone(supervisor.deferred_flush)
        with patch('orchd.app_worker.stop') as stop:
            self.assertEqual(core.auto_retry(self.con, self.rt, *self.rt.auto_requests[0])['status'], 'running')
            stop.assert_called_once()
        replaced = store.get_task(self.con, task['id'])
        self.assertEqual(replaced['model'], core.WORKER_MODELS['sonnet'])
        self.assertEqual(replaced['worktree'], task['worktree'])
        self.assertEqual(replaced['auto_retry_count'], 1)
        self.assertEqual(self.dispatch()['model'], core.WORKER_MODELS['sonnet'])

    def test_rate_limit_cooldown_expires_and_other_errors_do_not_count(self):
        task = self.dispatch()
        self.assertFalse(core.rate_limit_notice(self.con, self.rt, task['id'], 'network error', 'one'))
        self.assertTrue(core.rate_limit_notice(self.con, self.rt, task['id'], {'codexErrorInfo': 'rateLimitExceeded'}, 'one'))
        self.assertEqual(routing.select(self.con)['selected_alias'], 'sonnet')
        self.assertEqual(routing.select(self.con, now=time.time()+601)['selected_alias'], 'sol')

    def test_batch_starts_multiple_workers_group_and_filters(self):
        result = mcp_server.call('dispatch_batch', {'tasks': [self.args, dict(self.args, title='Second')]},
                                 'orch', self.con, self.rt)
        self.assertEqual(len(self.rt.started), 2)
        self.assertEqual({t['status'] for t in result['tasks']}, {'running'})
        self.assertEqual(len({t['worktree'] for t in result['tasks']}), 2)
        group = result['group_id']
        extra = self.dispatch()
        for t in result['tasks'] + [dict(task_id=extra['id'])]:
            core.progress(self.con, self.rt, t['task_id'], 'Synthetic')
        self.assertEqual(len(mcp_server.call('list_open', {'group_id': group}, 'orch', self.con, self.rt)), 2)
        self.assertEqual(len(core.inbox(self.con, 'orch', group)), 2)
        self.assertEqual(len(core.inbox(self.con, 'orch')), 1)

    def test_batch_limit_and_partial_failure(self):
        with self.assertRaises(ValueError):
            core.dispatch_batch(self.con, self.rt, orch_thread='orch', tasks=[self.args]*5)
        result = core.dispatch_batch(self.con, self.rt, orch_thread='orch',
                                    tasks=[self.args, dict(self.args, repo='missing')])
        self.assertEqual([t['status'] for t in result['tasks']], ['running', 'failed'])
        self.assertEqual(len(self.rt.started), 1)

    def test_batch_omitted_model_is_auto(self):
        args = {k: v for k, v in self.args.items() if k != 'model'}
        with patch.dict(os.environ, {'ORCHD_WORKER_MODEL': 'sonnet'}):
            result = core.dispatch_batch(self.con, self.rt, orch_thread='orch', tasks=[args])
        self.assertEqual(result['tasks'][0]['model'], core.WORKER_MODELS['sol'])
        self.assertIn('fallback', result['tasks'][0]['model_reason'])

    def test_unreadable_cache_falls_back_and_known_capacity_uses_shadow_score(self):
        with patch('orchd.quota.Cache.status', side_effect=OSError('unavailable')):
            self.assertIn('fallback', self.dispatch()['model_reason'])
        self.observe(80, 10)
        snapshot = quota.Cache().status()
        expected = quota.shadow(snapshot)['selected_alias']
        self.assertEqual(self.dispatch()['model'], core.WORKER_MODELS[expected])

    def test_startup_exception_does_not_record_provider_limit(self):
        with patch.object(self.rt, 'start_codex_worker', side_effect=RuntimeError("You’ve hit your usage limit. Please try again later.")):
            result = core.dispatch_batch(self.con, self.rt, orch_thread='orch', tasks=[self.args, self.args])
        self.assertEqual([t['status'] for t in result['tasks']], ['failed', 'failed'])
        self.assertEqual(store.get_task(self.con, result['tasks'][0]['task_id'])['status'], 'failed')
        self.assertEqual(result['tasks'][1]['status'], 'failed')
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM messages WHERE kind='rate_limit'").fetchone()[0], 0)

    def test_superseded_or_closed_worker_cannot_notify(self):
        task = self.dispatch()
        self.assertFalse(core.rate_limit_notice(self.con, self.rt, task['id'], {'code': 'usage_limit_exceeded'}, 'old', job_id='obsolete'))
        self.assertFalse(core.rate_limit_notice(self.con, self.rt, task['id'], {'code': 'usage_limit_exceeded'}, 'old', generation='obsolete'))
        store.update_task(self.con, task['id'], status='closed')
        self.assertFalse(core.rate_limit_notice(self.con, self.rt, task['id'], {'code': 'usage_limit_exceeded'}, 'old'))
        self.assertEqual(self.rt.woken, [])

    def test_real_process_smoke(self):
        result = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1]/'scripts/routing-smoke.py')],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"simultaneous_alive": true', result.stdout)
        self.assertIn('"distinct_git_worktrees": true', result.stdout)
        self.assertIn('"auto_retry_count": 1', result.stdout)
        self.assertIn('"replacement_alive": true', result.stdout)

    def test_explicit_model_only_notifies_and_shadow_stays_unapplied(self):
        task = self.dispatch(model='sol')
        core.rate_limit_notice(self.con, self.rt, task['id'], {'code': 'usage_limit_exceeded'}, 'explicit')
        self.assertEqual(self.rt.auto_requests, [])
        self.assertEqual(len(self.rt.woken), 1)
        self.assertFalse(task['auto_model'])
        msg = self.con.execute("SELECT body FROM messages WHERE task_id=? AND kind='dispatch'", (task['id'],)).fetchone()
        self.assertFalse(json.loads(msg['body'])['quota_shadow']['applied'])
        self.assertEqual(store.get_task(self.con, task['id'])['model'], core.WORKER_MODELS['sol'])

    def test_auto_retry_holds_queue_then_delivers_once_to_replacement(self):
        task = self.dispatch()
        store.add_message(self.con, task['id'], store.QUEUED, 'Synthetic pending answer')
        core.rate_limit_notice(self.con, self.rt, task['id'], {'code': 'usage_limit_exceeded'}, 'one')
        core.rate_limit_notice(self.con, self.rt, task['id'], {'code': 'usage_limit_exceeded'}, 'two')
        self.assertEqual(len(self.rt.auto_requests), 1)
        self.assertEqual(core.answer(self.con, self.rt, task['id'], flush=True)['status'], 'queued')
        self.assertEqual(self.rt.resumed, [])
        core.auto_retry(self.con, self.rt, *self.rt.auto_requests[0])
        self.assertEqual(store.pending_answer_count(self.con, task['id']), 0)
        self.assertIn('Synthetic pending answer', self.rt.sent[-1][2])
        reopened = store.connect()
        self.addCleanup(reopened.close)
        self.assertEqual(store.get_task(reopened, task['id'])['auto_retry_count'], 1)

    def test_auto_retry_failure_consumes_attempt_and_notifies_without_losing_work(self):
        task = self.dispatch()
        core.rate_limit_notice(self.con, self.rt, task['id'], {'code': 'usage_limit_exceeded'}, 'one')
        with patch.object(self.rt, 'claude_trusted', return_value=False):
            result = core.auto_retry(self.con, self.rt, *self.rt.auto_requests[0])
        self.assertEqual(result['status'], 'failed')
        current = store.get_task(self.con, task['id'])
        self.assertEqual(current['worktree'], task['worktree'])
        self.assertEqual(current['auto_retry_count'], 1)
        self.assertIn('[auto retry failed]', core.inbox(self.con, 'orch')[-1]['body'])
        self.assertEqual(core.auto_retry(self.con, self.rt, *self.rt.auto_requests[0])['status'], 'skipped')

    def test_auto_retry_refuses_newer_app_turn(self):
        task = self.dispatch()
        store.update_task(self.con, task['id'], backend='app-server', generation='g', active_turn=None)
        core.rate_limit_notice(self.con, self.rt, task['id'], {'code': 'usage_limit_exceeded'}, 'one', generation='g')
        store.update_task(self.con, task['id'], active_turn='new-native-turn')
        self.assertEqual(core.auto_retry(self.con, self.rt, *self.rt.auto_requests[0])['status'], 'skipped')
        self.assertEqual(store.get_task(self.con, task['id'])['auto_retry_count'], 0)

    def test_auto_review_uses_shadow_cross_provider_constraint(self):
        author = self.dispatch(model='sol')
        review = self.dispatch(verifies=author['id'], task_type='review')
        self.assertEqual(review['model'], core.WORKER_MODELS['sonnet'])
        self.assertIn('review requires another provider', review['model_reason'])
        with self.assertRaisesRegex(ValueError, 'author unknown'):
            self.dispatch(task_type='review')

    def test_app_rejection_defers_helper_until_delivery_lock_released(self):
        from orchd.app_transport import RPCError
        task = self.dispatch()
        store.update_task(self.con, task['id'], backend='app-server', generation='g', turn_state='idle')
        store.add_message(self.con, task['id'], store.QUEUED, 'Synthetic pending')
        supervisor = app_worker.Supervisor(self.con.execute('PRAGMA database_list').fetchone()[2], task['id'], 'g')
        self.addCleanup(supervisor.con.close)
        supervisor.rpc = Mock()
        supervisor.rpc.call.side_effect = RPCError({'code': -32000, 'message': 'Rejected',
            'data': {'type': 'usage_limit_exceeded'}})
        self.assertEqual(supervisor.flush()['status'], 'failed')
        self.assertEqual(self.rt.auto_requests, [])
        with patch('orchd.runtime.Runtime', return_value=self.rt):
            supervisor.handle_deferred_limit()
        self.assertEqual(len(self.rt.auto_requests), 1)
        self.assertEqual(supervisor.flush()['status'], 'queued')
        with patch('orchd.app_worker.stop'):
            self.assertEqual(core.auto_retry(self.con, self.rt, *self.rt.auto_requests[0])['status'], 'running')
        self.assertEqual(store.pending_answer_count(self.con, task['id']), 0)

    def test_reconcile_missed_failed_completion_schedules_retry(self):
        task = self.dispatch()
        store.update_task(self.con, task['id'], backend='app-server', generation='g')
        supervisor = app_worker.Supervisor(self.con.execute('PRAGMA database_list').fetchone()[2], task['id'], 'g')
        self.addCleanup(supervisor.con.close)
        supervisor.rpc = Mock()
        turns = [{'id': 'missed', 'status': 'failed', 'error': {'message': 'Limit reached', 'codexErrorInfo': 'usageLimitExceeded'}}]
        with patch.object(supervisor, 'history', return_value=turns), patch('orchd.runtime.Runtime', return_value=self.rt):
            supervisor.reconcile()
        self.assertEqual(len(self.rt.auto_requests), 1)
        self.assertIsNone(supervisor.deferred_flush)

    def test_competing_helpers_claim_one_automatic_attempt(self):
        task = self.dispatch()
        core.rate_limit_notice(self.con, self.rt, task['id'], {'code': 'usage_limit_exceeded'}, 'one')
        results, errors = [], []
        database = self.con.execute('PRAGMA database_list').fetchone()[2]
        def run():
            con = store.connect(database)
            try:
                results.append(core.auto_retry(con, self.rt, *self.rt.auto_requests[0])['status'])
            except Exception as exc:
                errors.append(str(exc))
            finally:
                con.close()
        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertTrue(all(not t.is_alive() for t in threads))
        self.assertEqual(errors, [])
        self.assertCountEqual(results, ['running', 'skipped'])
        self.assertEqual(store.get_task(self.con, task['id'])['auto_retry_count'], 1)

    def test_helper_waits_for_initial_dispatch_to_publish_running(self):
        task = self.dispatch()
        core.rate_limit_notice(self.con, self.rt, task['id'], {'code': 'usage_limit_exceeded'}, 'one')
        store.update_task(self.con, task['id'], status='starting')
        with patch.object(self.rt, 'sleep', side_effect=lambda _: store.update_task(self.con, task['id'], status='running')):
            self.assertEqual(core.auto_retry(self.con, self.rt, *self.rt.auto_requests[0])['status'], 'running')

    def test_exec_completion_waits_for_initial_job_identity(self):
        task = self.dispatch()
        store.update_task(self.con, task['id'], status='starting', job_id=None)
        log = Path(self.tmp.name)/'early.jsonl'
        log.write_text(json.dumps({'type':'turn.failed', 'error':{'message':"You’ve hit your usage limit. Please try again later."}})+'\n')
        with patch.object(self.rt, 'sleep', side_effect=lambda _: store.update_task(
                self.con, task['id'], status='running', job_id=task['job_id'])):
            core.auto_flush(self.con, self.rt, task['id'], int(task['job_id']), turn_log=log)
        self.assertEqual(len(self.rt.auto_requests), 1)

    def test_dispatch_never_probes_when_cache_is_empty_or_expired(self):
        # Empty cache, then expired cache: dispatch reads only, treats both providers as unknown.
        for expired in (False, True):
            with self.subTest(expired=expired), patch('orchd.quota._run') as run, \
                    patch('orchd.quota.probe') as probe, patch('orchd.quota.subprocess.Popen') as popen:
                if expired:
                    self.observe(99, 20, age=quota.TTL + 1)
                started = time.monotonic()
                task = self.dispatch()
                self.assertLess(time.monotonic() - started, 5)
                run.assert_not_called()
                probe.assert_not_called()
                popen.assert_not_called()
                self.assertEqual(task['model'], core.WORKER_MODELS['sol'])
                self.assertIn('fallback', task['model_reason'])

    def test_unknown_quota_wakes_background_refresher_without_waiting(self):
        with patch('orchd.quota.request_refresh') as wake:
            self.dispatch()
            wake.assert_called()
        self.observe(10, 20)
        with patch('orchd.quota.request_refresh') as wake:
            self.dispatch()
            wake.assert_not_called()  # fresh known quota needs no refresh
