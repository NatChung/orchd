import time
import tempfile
import unittest
import json
import os
from unittest.mock import patch
import io
import contextlib
import datetime
from pathlib import Path

from orchd import quota


REAL_CACHE = quota.Cache


class QuotaTest(unittest.TestCase):
    def test_claude_cli_small_future_observation_is_known_with_zero_age(self):
        now = datetime.datetime(2026, 12, 31, 22, 0, tzinfo=datetime.timezone.utc).timestamp()
        display = ('Current session: 5% used · resets Jan 1 at 1am (UTC)\n'
                   'Current week (all models): 11% used · resets Jan 2 at 12:30pm (UTC)')
        with tempfile.TemporaryDirectory() as tmp:
            cache = quota.Cache(Path(tmp) / 'quota.db')
            for skew in (.004, .028, 2):
                with self.subTest(skew=skew):
                    quota.claude_usage(cache, display, now=now + skew)
                    window = cache.status(now=now)['sonnet']['windows'][0]
                    self.assertEqual((window['state'], window['age_seconds'], window['used_percent']),
                                     ('known', 0, 5))
            quota.claude_usage(cache, display, now=now + 30)
            self.assertEqual(cache.status(now=now)['sonnet']['windows'][0]['reason'],
                             'invalid observation time')

    def test_claude_cli_receipt_has_five_minute_ttl_without_changing_sol(self):
        now = datetime.datetime(2026, 12, 31, 22, 0, tzinfo=datetime.timezone.utc).timestamp()
        display = ('Current session: 5% used · resets Jan 1 at 1am (UTC)\n'
                   'Current week (all models): 11% used · resets Jan 2 at 12:30pm (UTC)')
        with tempfile.TemporaryDirectory() as tmp:
            cache = quota.Cache(Path(tmp) / 'quota.db')
            quota.claude_usage(cache, display, now=now)
            cache.codex({'rateLimits': {'primary': {'usedPercent': 20,
                'windowDurationMins': 300, 'resetsAt': now + 10000}}}, now=now)
            for age in (63, quota.TTL):
                with self.subTest(age=age):
                    self.assertEqual(cache.status(now=now + age)['sonnet']['state'], 'known')
            for age in (quota.TTL + 1, quota.MAX_AGE):
                with self.subTest(age=age):
                    self.assertEqual(cache.status(now=now + age)['sonnet']['windows'][0]['reason'],
                                     'expired TTL')
            self.assertEqual(cache.status(now=now + quota.MAX_AGE + 1)['sonnet']['windows'][0]['reason'], 'stale')
            self.assertEqual(cache.status(now=now + quota.TTL + 1)['sol']['windows'][0]['reason'], 'expired TTL')
            self.assertEqual(cache.status(now=now - .004)['sol']['windows'][0]['reason'],
                             'invalid observation time')
            # The reset boundary still wins over the longer CLI TTL.
            quota.claude_usage(cache, display, now=now + 10799)
            self.assertEqual(cache.status(now=now + 10800)['sonnet']['windows'][0]['reason'],
                             'reset passed; refresh required')

    def test_empty_rpc_payload_cannot_overwrite_known_quota(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = quota.Cache(Path(tmp) / 'quota.db')
            cache.codex({'rateLimits': {'primary': {'usedPercent': 52,
                'windowDurationMins': 10080, 'resetsAt': 2000}}}, now=1000)
            with self.assertRaisesRegex(ValueError, 'no rate limit snapshot'):
                cache.codex({}, now=1001)
            window = cache.status(now=1002)['sol']['windows'][0]
            self.assertEqual((window['state'], window['used_percent'], window['observed_at']),
                             ('known', 52, 1000))

    def test_exec_turn_collects_only_its_own_rollout_and_preserves_age(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'ORCHD_CODEX_SESSIONS': tmp}):
            root = Path(tmp)
            log = root / 'codex.jsonl'
            log.write_text('{"type":"thread.started","thread_id":"worker-1"}\n')
            event = {'timestamp': '1970-01-01T00:16:40Z', 'type': 'event_msg',
                     'payload': {'type': 'token_count', 'rate_limits': {'primary': {
                         'used_percent': 42, 'window_minutes': 300, 'resets_at': 2000}}}}
            (root / 'rollout-one-worker-1.jsonl').write_text(json.dumps(event) + '\n')
            event['payload']['rate_limits']['primary']['used_percent'] = 99
            (root / 'rollout-other-worker-2.jsonl').write_text(json.dumps(event) + '\n')
            cache = quota.Cache(root / 'quota.db')
            with patch('orchd.quota.time.time', return_value=1001):
                quota.collect_codex_turn(cache, log)
            window = cache.status(now=1002)['sol']['windows'][0]
            self.assertEqual((window['used_percent'], window['age_seconds']), (42, 2))
            self.assertEqual(cache.status(now=1000 + quota.MAX_AGE + 1)['sol']['windows'][0]['reason'], 'stale')

    def test_app_turn_force_read_bypasses_attempt_ttl_and_idle_read_is_throttled(self):
        from unittest.mock import Mock
        from orchd.app_worker import Supervisor
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'ORCHD_HOME': tmp}):
            supervisor = Supervisor.__new__(Supervisor)
            supervisor.con = None
            supervisor.task_id = 'worker'
            supervisor.rpc = Mock()
            supervisor.rpc.call.return_value = {'rateLimits': {'primary': {
                'usedPercent': 20, 'windowDurationMins': 300, 'resetsAt': 2000}}}
            with patch('orchd.quota.time.time', return_value=1000):
                supervisor.collect_quota(force=True)
                supervisor.collect_quota(force=True)
                supervisor.collect_quota()
            self.assertEqual(supervisor.rpc.call.call_count, 2)
            self.assertEqual(quota.Cache().status(now=1001)['sol']['state'], 'known')
            with patch('orchd.quota.time.time', return_value=1031):
                supervisor.collect_quota()
            self.assertEqual(quota.Cache().status(now=1062)['sol']['state'], 'known')

    def test_app_turn_event_writes_rate_limit_receipt(self):
        from unittest.mock import Mock
        from orchd.app_worker import Supervisor
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'ORCHD_HOME': tmp}):
            supervisor = Supervisor.__new__(Supervisor)
            supervisor.con = None
            supervisor.task_id = 'worker'
            supervisor.rpc = Mock()
            supervisor.rpc.call.return_value = {'rateLimits': {'primary': {
                'usedPercent': 32, 'windowDurationMins': 300, 'resetsAt': 2000}}}
            with patch.object(supervisor, 'current', return_value=True), \
                    patch.object(supervisor, 'task', return_value={'session_id': 'worker'}), \
                    patch.object(supervisor, 'update'), patch('orchd.app_worker.store.add_message'), \
                    patch('orchd.quota.time.time', return_value=1000):
                supervisor.event({'method': 'turn/started', 'params': {
                    'threadId': 'worker', 'turn': {'id': 'turn'}}})
            self.assertEqual(quota.Cache().status(now=1001)['sol']['windows'][0]['used_percent'], 32)

    def test_failed_quota_read_does_not_break_app_turn(self):
        from unittest.mock import Mock
        from orchd.app_worker import Supervisor
        supervisor = Supervisor.__new__(Supervisor)
        supervisor.rpc = Mock()
        supervisor.rpc.call.side_effect = TimeoutError('offline')
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'ORCHD_HOME': tmp}), \
                patch('orchd.quota.refresh', side_effect=TimeoutError('offline')):
            supervisor.collect_quota(force=True)

    def test_claude_timeout_is_unknown_without_numbers(self):
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            cache = quota.Cache(Path(tmp) / 'quota.db')
            with patch('orchd.quota._run', side_effect=subprocess.TimeoutExpired('claude', 15)):
                quota.probe(cache, 'sonnet')
            status = cache.status()['sonnet']
            self.assertEqual(status['unknown_reasons'], ['Claude /usage unavailable: TimeoutExpired'])
            self.assertIsNone(status['windows'][0]['used_percent'])

    def test_claude_usage_native_result_and_concrete_unknown(self):
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as tmp:
            cache = quota.Cache(Path(tmp) / 'quota.db')
            now = datetime.datetime(2026, 12, 31, 22, 0, tzinfo=datetime.timezone.utc).timestamp()
            display = ('Current session: 13% used · resets Jan 1 at 1am (UTC)\n'
                       'Current week (all models): 5% used · resets Jan 2 at 12:30pm (UTC)')
            result = Mock(returncode=0, stdout=json.dumps(dict(result=display, num_turns=0,
                           local_command='usage', duration_api_ms=0)))
            with patch('orchd.quota._run', return_value=result) as run, \
                    patch('orchd.quota.time.time', return_value=now):
                quota.probe(cache, 'sonnet')
            status = cache.status(now=now + 1)['sonnet']
            self.assertEqual(status['state'], 'known')
            self.assertEqual([w['used_percent'] for w in status['windows']], [13, 5])
            self.assertEqual(status['windows'][0]['reset_at'], now + 10800)
            self.assertIn('{"disableAllHooks":true}', run.call_args.args[0])
            result.stdout = json.dumps(dict(result='Unknown command: usage', num_turns=0,
                                           local_command='usage', duration_api_ms=0))
            with patch('orchd.quota._run', return_value=result), \
                    patch('orchd.quota.time.time', return_value=now + 2):
                quota.probe(cache, 'sonnet')
            status = cache.status(now=now + 3)['sonnet']
            self.assertEqual(status['state'], 'known')
            self.assertEqual([w['used_percent'] for w in status['windows']], [13, 5])
            self.assertEqual(status['windows'][0]['observed_at'], now)

    def test_claude_usage_refuses_inference_or_guessed_resets(self):
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as tmp:
            cache = quota.Cache(Path(tmp) / 'quota.db')
            result = Mock(returncode=0, stdout=json.dumps(dict(result='13%', num_turns=1)))
            with patch('orchd.quota._run', return_value=result):
                quota.probe(cache, 'sonnet')
            self.assertIn('zero turns required', cache.status()['sonnet']['unknown_reasons'][0])
            with self.assertRaises(ValueError):
                quota.claude_usage(cache, 'Current session: 13% used · resets tomorrow')

    def test_claude_worker_configures_session_only_quota_hooks(self):
        from orchd.runtime import Runtime
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'ORCHD_HOME': tmp}):
            runtime = Runtime()
            with patch.object(runtime, 'start_claude') as start:
                runtime.start_worker('/worktree', '/tmp/socket', 'brief', 'claude-sonnet-5-5')
            args = start.call_args.args[3]
            settings = json.loads(args[args.index('--settings') + 1])
            self.assertEqual(set(settings['hooks']), {'SessionStart', 'UserPromptSubmit', 'Stop', 'StopFailure'})
            command = settings['hooks']['Stop'][0]['hooks'][0]['command']
            self.assertIn('quota --probe sonnet', command)
            self.assertIn('ORCHD_HOME=' + tmp, command)

    def test_codex_read_and_updated_preserve_buckets(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = quota.Cache(Path(tmp) / 'quota.db')
            cache.codex({'rateLimitsByLimitId': {
                'codex': {'primary': {'usedPercent': 42, 'windowDurationMins': 10080,
                                      'resetsAt': 2000}, 'secondary': None},
                'other': {'primary': {'usedPercent': 25, 'windowDurationMins': 15,
                                     'resetsAt': 1800}}}}, now=1000)
            windows = cache.status(now=1001)['sol']['windows']
            self.assertEqual([(w['limit_id'], w['used_percent']) for w in windows],
                             [('codex', 42), ('other', 25)])
            self.assertEqual(windows[0]['window_seconds'], 604800)
            cache.codex({'rateLimits': {'limitId': 'codex', 'primary': {
                'usedPercent': 43, 'windowDurationMins': 10080, 'resetsAt': 2000}}},
                source='codex.updated', now=1002)
            self.assertEqual(cache.status(now=1003)['sol']['windows'][0]['used_percent'], 43)

    def test_claude_receipts_expire_without_guessing(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = quota.Cache(Path(tmp) / 'quota.db')
            cache.claude({'rate_limits': {'five_hour': {'used_percentage': 23.5, 'resets_at': 2000},
                                         'seven_day': {'used_percentage': 41.2, 'resets_at': 3000}}}, now=1000)
            self.assertEqual(cache.status(now=1000 + quota.TTL)['sonnet']['state'], 'known')
            window = cache.status(now=1000 + quota.TTL + 1)['sonnet']['windows'][0]
            self.assertEqual((window['state'], window['used_percent'], window['reason']),
                             ('unknown', None, 'expired TTL'))
            self.assertEqual(cache.status(now=1000 + quota.MAX_AGE + 1)['sonnet']['windows'][0]['reason'], 'stale')
            cache.claude({'rate_limit_info': {'rate_limit_type': 'seven_day_sonnet',
                                             'utilization': .5, 'resets_at': 4000}}, sdk=True, now=1302)
            self.assertEqual(next(w for w in cache.status(now=1303)['sonnet']['windows'] if w['name'] == 'seven_day_sonnet')['used_percent'], 50)
            cache.claude({'rate_limit_info': {'rate_limit_type': 'seven_day_sonnet',
                                             'status': 'allowed', 'resets_at': 4000}}, sdk=True, now=1304)
            self.assertIsNone(next(w for w in cache.status(now=1305)['sonnet']['windows'] if w['name'] == 'seven_day_sonnet')['used_percent'])

    def test_session_fallback_uses_event_time_and_not_token_totals(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sessions = root / 'sessions'
            sessions.mkdir()
            (sessions / 'one.jsonl').write_text(json.dumps({'timestamp': '1970-01-01T00:16:40Z',
                'type': 'event_msg', 'payload': {'type': 'token_count', 'rate_limits': {
                    'primary': {'used_percent': 42, 'window_minutes': 10080, 'resets_at': 2000}}}}) + '\n')
            cache = quota.Cache(root / 'quota.db')
            quota.refresh(cache, sessions=sessions, now=1001)
            self.assertEqual(cache.status(now=1002)['sol']['windows'][0]['age_seconds'], 2)
            self.assertEqual(cache.status(now=1000 + quota.TTL + 1)['sol']['windows'][0]['state'], 'unknown')

    def test_shadow_dispatch_preserves_explicit_model_and_records_review_rule(self):
        from orchd import core, store
        from tests.test_orchd import FakeRuntime
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'ORCHD_HOME': tmp}):
            con = store.connect()
            self.addCleanup(con.close)
            args = dict(orch_thread='orch', repo='demo', title='T', instructions='do', done_when='ok',
                        model='sol', backend='exec', model_reason='explicit', task_type='code')
            author = core.dispatch(con, FakeRuntime(), **args)
            review = core.dispatch(con, FakeRuntime(), **dict(args, task_type='review', verifies=author['id']))
            self.assertEqual(review['model'], core.WORKER_MODELS['sol'])
            msg = con.execute("SELECT body FROM messages WHERE task_id=? AND kind='dispatch'", (review['id'],)).fetchone()
            shadow = json.loads(msg['body'])['quota_shadow']
            self.assertIsNone(shadow['selected_alias'])
            self.assertIn('review', shadow['reason'])
            store.update_task(con, author['id'], model=None)
            unknown = core.dispatch(con, FakeRuntime(), **dict(args, verifies=author['id']))
            msg = con.execute("SELECT body FROM messages WHERE task_id=? AND kind='dispatch'", (unknown['id'],)).fetchone()
            self.assertIsNone(json.loads(msg['body'])['quota_shadow']['selected_alias'])

    def test_cli_receipt_and_mcp_status_share_snapshot(self):
        from orchd.cli import main
        from orchd import mcp_server
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'ORCHD_HOME': tmp}):
            receipt = json.dumps({'rate_limits': {'five_hour': {'used_percentage': 20, 'resets_at': 4000},
                'seven_day': {'used_percentage': 50, 'resets_at': 5000}}})
            with patch('sys.stdin', io.StringIO(receipt)), patch('orchd.quota.time.time', return_value=1000), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(main(['quota', '--ingest', 'claude-statusline']), 0)
                self.assertEqual(json.loads(out.getvalue())['sonnet']['state'], 'known')
                quota.Cache().record_attempt('sonnet', 1000, .5,
                    dict(error_class='TimeoutError', message='timeout'))
                with contextlib.redirect_stdout(io.StringIO()) as diagnostic_out:
                    self.assertEqual(main(['quota']), 0)
                status = mcp_server.call('quota_status', {}, None, None, None)
                self.assertEqual(status, json.loads(diagnostic_out.getvalue()))
                self.assertEqual(status['sonnet']['refresh']['error']['message'], 'timeout')
                self.assertEqual(status['sonnet']['windows'][0]['used_percent'], 20)

    def test_app_server_account_events_are_not_thread_filtered(self):
        from orchd.app_worker import Supervisor
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'ORCHD_HOME': tmp}):
            supervisor = Supervisor.__new__(Supervisor)
            with patch.object(supervisor, 'current', return_value=True):
                supervisor.event({'method': 'account/rateLimits/updated', 'params': {'rateLimits': {
                    'primary': {'usedPercent': 30, 'windowDurationMins': 300, 'resetsAt': 4000}}}})
            self.assertEqual(quota.Cache().status(now=1000)['sol']['windows'][0]['source'], 'codex.updated')

    def test_missing_invalid_and_reset_windows_are_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = quota.Cache(Path(tmp) / 'quota.db')
            self.assertEqual(cache.status(now=1000)['sol']['state'], 'unknown')
            for used, reset, duration in [(None, 2000, 300), (True, 2000, 300),
                                          (101, 2000, 300), (10, 1000, 300), (10, 2000, None)]:
                cache.codex({'rateLimits': {'primary': {'usedPercent': used,
                    'windowDurationMins': duration, 'resetsAt': reset}}}, now=1000)
                self.assertEqual(cache.status(now=1000)['sol']['state'], 'unknown')

    def test_read_refresh_is_single_flight_and_does_not_infer(self):
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as tmp:
            cache = quota.Cache(Path(tmp) / 'quota.db')
            rpc = Mock()
            rpc.call.return_value = {'rateLimits': {'primary': {'usedPercent': 20,
                'windowDurationMins': 300, 'resetsAt': 2000}}}
            quota.refresh(cache, rpc=rpc, now=1000)
            quota.refresh(cache, rpc=rpc, now=1010)
            rpc.call.assert_called_once_with('account/rateLimits/read', {}, timeout=3)
            self.assertEqual(cache.status(now=1010)['sol']['windows'][0]['source'], 'codex.read')

    def test_shadow_requires_known_capacity_and_cross_provider_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = quota.Cache(Path(tmp) / 'quota.db')
            cache.codex({'rateLimits': {'primary': {'usedPercent': 96,
                'windowDurationMins': 300, 'resetsAt': 2000}}}, now=1000)
            cache.claude({'rate_limits': {'five_hour': {'used_percentage': 20, 'resets_at': 2000},
                'seven_day': {'used_percentage': 50, 'resets_at': 5000}}}, now=1000)
            snapshot = cache.status(now=1001)
            self.assertEqual(quota.shadow(snapshot, now=1001)['selected_alias'], 'sonnet')
            self.assertIsNone(quota.shadow(snapshot, task_type='review', author_model='claude-sonnet-5-5',
                                          now=1001)['selected_alias'])
            self.assertIsNone(quota.shadow(snapshot, backend='app-server', task_type='review',
                author_model='gpt-6.1-sol', now=1001)['selected_alias'])

    def test_older_observations_cannot_replace_newer_and_status_is_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'quota.db'
            cache = quota.Cache(path)
            payload = {'rateLimits': {'primary': {'usedPercent': 40,
                'windowDurationMins': 300, 'resetsAt': 2000}}}
            cache.codex(payload, now=1000)
            cache.codex(payload, source='old', now=999)
            with patch.object(cache, 'connect', side_effect=AssertionError('read must not initialize DB')):
                self.assertEqual(cache.status(now=1001)['sol']['windows'][0]['source'], 'codex.read')

    def test_missing_bucket_cannot_be_hidden_by_another_known_bucket(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = quota.Cache(Path(tmp) / 'quota.db')
            cache.codex({'rateLimitsByLimitId': {'codex': {}, 'other': {'primary': {
                'usedPercent': 10, 'windowDurationMins': 300, 'resetsAt': 2000}}}}, now=1000)
            self.assertEqual(cache.status(now=1001)['sol']['state'], 'unknown')

    def _sonnet_known(self, cache, now):
        cache.claude({'rate_limits': {'five_hour': {'used_percentage': 20, 'resets_at': now + 2000},
            'seven_day': {'used_percentage': 50, 'resets_at': now + 5000}}}, now=now)

    def _score_pick(self, sol, sonnet):
        """sol: [used%...] per window (5h, 7d); sonnet likewise; reset times far apart on purpose."""
        now = 1000
        snap = {}
        for p, used in (('sol', sol), ('sonnet', sonnet)):
            snap[p] = dict(state='known', windows=[dict(state='known', used_percent=u, reset_at=now + r)
                                                   for u, r in zip(used, (3600, 600000))])
        return quota.shadow(snap, now=now, applied=True)

    def test_remaining_percent_balances_usage(self):
        self.assertEqual(self._score_pick([1], [10, 14])['selected_alias'], 'sol')
        self.assertEqual(self._score_pick([30], [90, 20])['selected_alias'], 'sol')
        result = self._score_pick([60], [10, 10])
        self.assertEqual(result['selected_alias'], 'sonnet')
        self.assertIn('remaining-percent score', result['reason'])

    def test_remaining_percent_tie_margin_prefers_sol(self):
        self.assertEqual(self._score_pick([40], [36])['selected_alias'], 'sol')
        self.assertEqual(self._score_pick([40], [35])['selected_alias'], 'sol')   # exactly 5 apart
        self.assertEqual(self._score_pick([40], [34])['selected_alias'], 'sonnet')  # 6 apart
        self.assertEqual(self._score_pick([60], [40])['selected_alias'], 'sonnet')

    def test_remaining_percent_keeps_95_cap(self):
        self.assertEqual(self._score_pick([96], [80])['selected_alias'], 'sonnet')

    def test_unknown_sol_is_preferred_over_known_sonnet(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = quota.Cache(Path(tmp) / 'quota.db')
            cache.codex({'rateLimits': {'primary': {'usedPercent': 10,
                'windowDurationMins': 300, 'resetsAt': 100000}}}, now=1000)
            self._sonnet_known(cache, 1000 + quota.TTL + 100)
            now = 1000 + quota.TTL + 101
            snapshot = cache.status(now=now)
            self.assertEqual(snapshot['sol']['unknown_reasons'], ['expired TTL'])
            for applied in (True, False):
                result = quota.shadow(snapshot, now=now, applied=applied)
                self.assertEqual(result['selected_alias'], 'sol')
                self.assertIn('prefer sol', result['reason'])
            self.assertEqual(quota.shadow(cache.status(now=1000 + quota.MAX_AGE + 101), now=now)['selected_alias'], 'sol')

    def test_recent_limited_unknown_sol_selects_sonnet(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = quota.Cache(Path(tmp) / 'quota.db')
            self._sonnet_known(cache, 1000)
            result = quota.shadow(cache.status(now=1001), now=1001, applied=True, recent_limited=('sol',))
            self.assertEqual(result['selected_alias'], 'sonnet')

    def test_known_full_sol_is_not_preferred_when_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = quota.Cache(Path(tmp) / 'quota.db')
            cache.codex({'rateLimits': {'primary': {'usedPercent': 96,
                'windowDurationMins': 300, 'resetsAt': 5000},
                'secondary': None}}, now=1000)
            self._sonnet_known(cache, 1000)
            self.assertEqual(quota.shadow(cache.status(now=1001), now=1001)['selected_alias'], 'sonnet')

    def test_ttl_boundary_and_period_margin(self):
        self.assertGreaterEqual(quota.TTL, 3 * quota.BACKGROUND_PERIOD)
        self.assertGreater(quota.MAX_AGE, quota.TTL)
        with tempfile.TemporaryDirectory() as tmp:
            cache = quota.Cache(Path(tmp) / 'quota.db')
            cache.codex({'rateLimits': {'primary': {'usedPercent': 10,
                'windowDurationMins': 300, 'resetsAt': 100000}}}, now=1000)
            self.assertEqual(cache.status(now=1000 + quota.TTL)['sol']['state'], 'known')
            self.assertEqual(cache.status(now=1000 + quota.TTL + 1)['sol']['state'], 'unknown')

    def test_background_refresh_updates_cache_and_tolerates_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = quota.Cache(Path(tmp) / 'quota.db')
            calls = []

            def fake_probe(c, provider, binary=None):
                calls.append(provider)
                if provider == 'sol':
                    c.codex({'rateLimits': {'primary': {'usedPercent': 10,
                        'windowDurationMins': 300, 'resetsAt': 4000}}}, now=1000)
                else:
                    raise OSError('claude missing')
            with patch.object(quota, 'probe', fake_probe):
                self.assertTrue(quota.refresh_background(cache, now=1000, binary='x'))
                self.assertEqual(calls, ['sol', 'sonnet'])
                self.assertEqual(cache.status(now=1001)['sol']['state'], 'known')
                self.assertFalse(quota.refresh_background(cache, now=1001))
                self.assertTrue(quota.refresh_background(cache, now=1000 + quota.BACKGROUND_PERIOD))

    def test_provider_diagnostics_survive_repeated_failure_and_recovery(self):
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            cache = quota.Cache(Path(tmp) / 'quota.db')
            self._sonnet_known(cache, 1000)
            for now in range(4000, 4900, 60):
                with patch.object(quota, 'refresh') as refresh, \
                        patch.object(quota, '_run', return_value=subprocess.CompletedProcess(
                            [], 1, '', 'Authorization: Bearer SECRET network timeout')), \
                        patch.object(quota.time, 'time', return_value=now):
                    refresh.side_effect = lambda *a, **k: cache.codex({'rateLimits': {'primary': {
                        'usedPercent': 10, 'windowDurationMins': 300, 'resetsAt': 100000}}}, now=now)
                    quota.refresh_background(cache, now=now)
            status = cache.status(now=4900)
            diag = status['sonnet']['refresh']
            self.assertEqual(diag['last_attempt_at'], 4840)
            self.assertIsNone(diag['last_success_at'])
            self.assertEqual(len(diag['history']), quota.DIAGNOSTIC_HISTORY)
            self.assertIn('timeout', diag['error']['message'])
            self.assertNotIn('SECRET', json.dumps(status))
            self.assertEqual(status['sonnet']['windows'][0]['observed_at'], 1000)
            self.assertIsNone(status['sol']['refresh']['error'])
            now = datetime.datetime(2026, 12, 31, 22, tzinfo=datetime.timezone.utc).timestamp()
            display = ('Current session: 13% used · resets Jan 1 at 1am (UTC)\n'
                       'Current week (all models): 5% used · resets Jan 2 at 12:30pm (UTC)')
            result = subprocess.CompletedProcess([], 0, json.dumps(dict(result=display,
                num_turns=0, local_command='usage', duration_api_ms=0)), '')
            with patch.object(quota, '_run', return_value=result), \
                    patch.object(quota.time, 'time', return_value=now):
                quota.probe(cache, 'sonnet')
            diag = quota.Cache(cache.path).status(now=now)['sonnet']['refresh']
            self.assertIsNone(diag['error'])
            self.assertEqual(diag['last_success_at'], now)
            self.assertEqual(len(diag['history']), quota.DIAGNOSTIC_HISTORY)
            self.assertIsNotNone(diag['history'][-2]['error'])
            with patch.object(quota, '_run', side_effect=TimeoutError('SECRET')), \
                    patch.object(quota.time, 'time', return_value=now + 60):
                quota.probe(cache, 'sonnet')
            status = cache.status(now=now + 60)['sonnet']
            self.assertEqual(status['refresh']['last_success_at'], now)
            self.assertIsNotNone(status['refresh']['error'])
            self.assertEqual(status['windows'][0]['observed_at'], now)

    def test_diagnostic_read_is_backward_compatible_and_read_only(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            cache = quota.Cache(Path(tmp) / 'quota.db')
            self.assertIsNone(cache.status()['sonnet']['refresh']['last_attempt_at'])
            self.assertFalse(cache.path.exists())
            with sqlite3.connect(cache.path) as con:
                con.execute('CREATE TABLE buckets (provider TEXT, id TEXT, observed REAL, source TEXT, windows TEXT)')
            before = cache.path.read_bytes()
            self.assertEqual(cache.status()['sonnet']['refresh']['history'], [])
            self.assertEqual(cache.path.read_bytes(), before)
            cache.path.with_suffix('.periodic.lock').write_text(json.dumps(dict(attempt=1000, success=1000)))
            with patch.object(quota, 'probe') as probe:
                self.assertFalse(quota.refresh_background(cache, now=1001))
                probe.assert_not_called()
            cache.record_attempt('sonnet', 1000, .25, dict(error_class='TimeoutError', message='timeout'))
            with patch.dict(os.environ, {'ORCHD_QUOTA_REFRESH_SECONDS': '0'}):
                status = cache.status(now=1400)['sonnet']
                self.assertFalse(status['refresher']['enabled'])
                self.assertFalse(status['refresher']['process_running'])
            self.assertTrue(status['refresher']['scheduling_gap'])
            self.assertEqual(status['refresher']['seconds_since_attempt'], 400)
            self.assertEqual(status['refresh']['duration_seconds'], .25)

    def test_refresh_exception_is_diagnostic_and_preserves_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = quota.Cache(Path(tmp) / 'quota.db')
            cache.codex({'rateLimits': {'primary': {'usedPercent': 10,
                'windowDurationMins': 300, 'resetsAt': 10000}}}, now=1000)
            with patch.object(quota, 'refresh', side_effect=OSError('private path SECRET')), \
                    patch.object(quota, 'probe', return_value=False):
                quota.refresh_background(cache, now=4000)
            status = cache.status(now=4000)['sol']
            self.assertEqual(status['windows'][0]['observed_at'], 1000)
            self.assertEqual(status['refresh']['error']['error_class'], 'OSError')
            self.assertNotIn('SECRET', json.dumps(status))

    def test_dispatch_reads_cache_without_probing_sol(self):
        real = quota.Cache
        with tempfile.TemporaryDirectory() as tmp, patch.object(quota, 'probe') as probe, \
                patch.object(quota, 'Cache', lambda: real(Path(tmp) / 'quota.db')):
            self._sonnet_known(quota.Cache(), 1000)
            result = quota.dispatch_shadow(applied=True, now=1001)
            self.assertEqual(result['selected_alias'], 'sol')
            probe.assert_not_called()

    def _fake_sol_probe(self, calls):
        def fake_probe(c, provider, binary=None):
            calls.append(provider)
            if provider == 'sol':
                c.codex({'rateLimits': {'primary': {'usedPercent': 10,
                    'windowDurationMins': 300, 'resetsAt': 10 ** 6}}}, now=self.clock)
        return fake_probe

    def test_refresh_is_deduplicated_for_a_whole_period_across_gates(self):
        # t=1000 refreshes; t=1150 (same 300s period, different caller) must not.
        with tempfile.TemporaryDirectory() as tmp:
            cache = quota.Cache(Path(tmp) / 'quota.db')
            calls = []
            self.clock = 1000
            with patch.object(quota, 'probe', self._fake_sol_probe(calls)):
                self.assertTrue(quota.refresh_background(cache, now=1000))
                # Sonnet alone retries after its failed attempt; healthy sol stays gated.
                self.assertFalse(quota.refresh_background(cache, now=1059))
                self.assertTrue(quota.refresh_background(cache, now=1150))
                self.assertTrue(quota.refresh_background(cache, now=1000 + quota.BACKGROUND_PERIOD - 1))
                self.assertEqual(calls.count('sol'), 1)
                self.assertEqual(calls.count('sonnet'), 3)
                self.assertTrue(quota.refresh_background(cache, now=1000 + quota.BACKGROUND_PERIOD))
                self.assertEqual(calls.count('sol'), 1)
                self.assertEqual(calls.count('sonnet'), 3)

    def test_failed_refresh_retries_only_after_probe_interval(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = quota.Cache(Path(tmp) / 'quota.db')
            calls = []
            with patch.object(quota, 'probe', lambda c, p, b=None: calls.append(p)):
                self.assertFalse(quota.refresh_background(cache, now=1000))
                n = len(calls)
                self.assertFalse(quota.refresh_background(cache, now=1000 + quota.SONNET_PROBE_INTERVAL - 1))
                self.assertEqual(len(calls), n)
                quota.refresh_background(cache, now=1000 + quota.SONNET_PROBE_INTERVAL)
                self.assertGreater(len(calls), n)

    def test_concurrent_refresh_never_overlaps_and_probes_once(self):
        import threading
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'quota.db'
            calls, inside, gate = [], threading.Event(), threading.Event()
            self.clock = 1000
            base = self._fake_sol_probe(calls)

            def slow(c, provider, binary=None):
                base(c, provider, binary)
                if provider == 'sonnet':
                    inside.set()
                    gate.wait(5)
            results = []
            with patch.object(quota, 'probe', slow):
                first = threading.Thread(target=lambda: results.append(quota.refresh_background(quota.Cache(path), now=1000)))
                first.start()
                self.assertTrue(inside.wait(5))
                # A second gate (another process/thread) while the first is mid-probe backs off.
                self.assertFalse(quota.refresh_background(quota.Cache(path), now=1000))
                gate.set()
                first.join(5)
                self.assertFalse(quota.refresh_background(quota.Cache(path), now=1001))
            self.assertEqual(results, [True])
            self.assertEqual(calls.count('sonnet'), 1)

    def test_request_refresh_wakes_running_background_only(self):
        self.assertFalse(quota.request_refresh())  # no refresher in this process
        with tempfile.TemporaryDirectory() as tmp:
            cache = quota.Cache(Path(tmp) / 'quota.db')
            seen = []
            with patch.object(quota, 'refresh_background', lambda c, **k: seen.append(1)):
                quota.start_background(cache, interval=3600)
                try:
                    deadline = time.time() + 5
                    while not seen and time.time() < deadline:
                        time.sleep(.01)
                    self.assertTrue(quota.request_refresh())
                    deadline = time.time() + 5
                    while len(seen) < 2 and time.time() < deadline:
                        time.sleep(.01)
                    self.assertEqual(len(seen), 2)
                finally:
                    quota.stop_background()
            self.assertFalse(quota.request_refresh())

    def _fake_claude(self, tmp):
        """Executable that records its pid then sleeps, standing in for a hung `claude /usage`."""
        pidfile = Path(tmp) / 'pid'
        script = Path(tmp) / 'claude'
        script.write_text(f'#!/bin/sh\necho $$ > {pidfile}\nsleep 60\n')
        script.chmod(0o755)
        return script, pidfile

    @staticmethod
    def _alive(pid):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True

    def test_thread_start_failure_is_tolerated_by_serve_and_stop(self):
        from orchd import mcp_server
        out = io.StringIO()
        with patch('threading.Thread.start', side_effect=RuntimeError('cannot start new thread')), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertIsNone(quota.start_background(interval=3600))
            with tempfile.TemporaryDirectory() as tmp:
                status = quota.Cache(Path(tmp) / 'quota.db').status()['sonnet']['refresher']
                self.assertFalse(status['process_running'])
                self.assertEqual(status['startup_error']['error_class'], 'RuntimeError')
            mcp_server.serve(stdin=io.StringIO('{"jsonrpc":"2.0","id":1,"method":"ping"}\n'),
                             stdout=out, con=object(), rt=object())
        self.assertIn('"id": 1', out.getvalue())
        quota.stop_background()  # safe with no started thread
        self.assertIsNone(quota._background)
        quota._background = quota.threading.Thread(target=lambda: None)
        quota.stop_background()  # must not raise "cannot join thread before it is started"
        self.assertIsNone(quota._background)

    def test_stop_background_terminates_probe_subprocess_and_joins_thread(self):
        with tempfile.TemporaryDirectory() as tmp:
            script, pidfile = self._fake_claude(tmp)
            cache = quota.Cache(Path(tmp) / 'quota.db')
            with patch.dict(os.environ, {'ORCHD_CLAUDE': str(script)}), \
                    patch.object(quota, 'refresh', lambda *a, **k: None):
                thread = quota.start_background(cache, interval=3600)
                deadline = time.time() + 10
                while not pidfile.exists() and time.time() < deadline:
                    time.sleep(.02)
                self.assertTrue(pidfile.exists(), 'probe subprocess never started')
                pid = int(pidfile.read_text())
                self.assertTrue(self._alive(pid))
                started = time.time()
                quota.stop_background(timeout=10)
                self.assertLess(time.time() - started, 10)
                self.assertFalse(thread.is_alive())
                deadline = time.time() + 5
                while self._alive(pid) and time.time() < deadline:
                    time.sleep(.02)
                self.assertFalse(self._alive(pid), 'probe subprocess outlived stop_background')
                # A killed probe is not recorded as an observation.
                self.assertEqual(cache.status()['sonnet']['windows'], [])

    def test_serve_end_stops_background_and_probe(self):
        import io
        from orchd import mcp_server, store
        with tempfile.TemporaryDirectory() as tmp:
            script, pidfile = self._fake_claude(tmp)
            with patch.dict(os.environ, {'ORCHD_CLAUDE': str(script), 'ORCHD_HOME': tmp}), \
                    patch.object(quota, 'Cache', lambda: REAL_CACHE(Path(tmp) / 'quota.db')), \
                    patch.object(quota, 'refresh', lambda *a, **k: None):
                class Lines:  # stdin that waits for the probe to start, then ends
                    def __iter__(inner):
                        deadline = time.time() + 10
                        while not pidfile.exists() and time.time() < deadline:
                            time.sleep(.02)
                        return iter(())
                mcp_server.serve(stdin=Lines(), stdout=io.StringIO(), con=object(), rt=object())
                self.assertTrue(pidfile.exists())
                pid = int(pidfile.read_text())
                deadline = time.time() + 5
                while self._alive(pid) and time.time() < deadline:
                    time.sleep(.02)
                self.assertFalse(self._alive(pid))
                self.assertFalse(quota._background)
