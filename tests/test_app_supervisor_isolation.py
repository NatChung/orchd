"""Issue #107: the app-server supervisor and viewer must never import orchd from the worker's worktree."""
import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from orchd import app_worker, core, store
from orchd.runtime import Runtime
from tests.app_support import AppBase, FakeRPC

REPO = Path(__file__).resolve().parents[1]
FAKE = Path(__file__).parent / 'fixtures' / 'fake_app_server.py'
SHADOW = "raise RuntimeError('SHADOWED-ORCHD-LOADED')\n"


class IsolationBase(AppBase):
    def setUp(self):
        super().setUp()
        self.worktree = self.root / 'wt'
        (self.worktree / 'orchd').mkdir(parents=True)
        (self.worktree / 'orchd' / '__init__.py').write_text(SHADOW)
        self.log = self.root / 'fake.log'
        self.fake = self.root / 'fake-codex'
        self.fake.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
        self.fake.chmod(0o700)
        store.update_task(self.con, self.id, codex_bin=str(self.fake), worktree=str(self.worktree))
        env = patch.dict(os.environ, {'ORCHD_HOME': str(self.root / 'state'), 'FAKE_APP_SERVER_LOG': str(self.log),
                                      'PYTHONPATH': str(REPO)})
        env.start()
        self.addCleanup(env.stop)

    def tearDown(self):
        self.stop_attempt()  # before AppBase closes the connection it needs
        super().tearDown()

    def logged(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def stop_attempt(self):
        try:
            app_worker.stop(self.con, store.get_task(self.con, self.id))
        except Exception:
            pass


class SupervisorIsolationTest(IsolationBase):
    def test_supervisor_ignores_worktree_orchd_and_still_delivers_answers(self):
        task = app_worker.start(self.con, self.rt, store.get_task(self.con, self.id), 'first prompt', 'gpt-6.1-sol')
        self.assertEqual(self.logged()[0]['cwd'], os.path.realpath(self.worktree))  # Codex still runs in the worktree
        deadline = time.monotonic() + 5
        while store.get_task(self.con, self.id)['turn_state'] != 'completed' and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertEqual(store.get_task(self.con, self.id)['turn_state'], 'completed')
        store.add_message(self.con, self.id, store.QUEUED, 'the answer')
        result = app_worker.control(store.get_task(self.con, self.id), 'flush', timeout=10)
        # Automatic maintenance may already own the delivery lane when flush arrives.
        self.assertIn(result['status'], ('delivered', 'queued'))
        deadline = time.monotonic() + 5
        while store.pending_answer_count(self.con, self.id) and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertEqual(store.pending_answer_count(self.con, self.id), 0)
        texts = [e['text'] for e in self.logged() if e['event'] == 'turn/start']
        self.assertEqual(len(texts), 2)
        self.assertIn('the answer', texts[1])
        self.assertEqual(app_worker.health(task), 'idle')

    def test_viewer_command_ignores_worktree_orchd(self):
        rt = Runtime()
        with patch.object(rt, 'run') as run, patch('orchd.runtime.worker_env', return_value={}):
            rt.open_app_viewer(str(self.db), store.get_task(self.con, self.id))
        cmd = run.call_args.args[0]
        tail = cmd[cmd.index('-e') + 1:]
        self.assertIn('-P', tail)
        self.assertLess(tail.index('-P'), tail.index('-m'))
        self.assertIn(f"--working-directory={self.worktree}", cmd)
        # Run exactly what the terminal would, in the poisoned worktree: it must reach orchd's own code.
        proc = subprocess.run([*tail[:tail.index('viewer') + 1], '/nonexistent', 'x', 'y'], cwd=self.worktree,
                              capture_output=True, text=True, timeout=30)
        self.assertNotIn('SHADOWED-ORCHD-LOADED', proc.stderr)
        self.assertNotIn(str(self.worktree), proc.stderr)  # failed inside orchd's own store, not the shadow


class FailureVisibilityTest(AppBase):
    def test_answer_reports_structured_supervisor_error_when_control_times_out(self):
        store.update_task(self.con, self.id, note='close-pending: keep me')
        with patch.object(app_worker, 'control', side_effect=TimeoutError('timed out')), \
                patch.object(app_worker, 'health_detail', return_value=('idle', 'ImportError: cannot import name X')):
            result = core.answer(self.con, self.rt, self.id, 'hi')
        self.assertIn('ImportError: cannot import name X', result['error'])

    def test_supervisor_still_answers_control_when_maintenance_fails(self):
        sup = self.supervisor
        with patch.object(sup, 'collect_quota', side_effect=ImportError('boom')):
            sup.maintain()
        self.assertIn('ImportError: boom', store.get_task(self.con, self.id)['note'])
        self.assertIn('ImportError: boom', sup.command({'command': 'health', 'generation': self.gen})
                      ['supervisor_error'])

    def test_supervisor_preloads_lazily_imported_modules(self):
        code = ("import sys; from orchd import app_worker; app_worker.preload();"
                "print(all(m in sys.modules for m in ('orchd.core','orchd.quota','orchd.routing')))")
        out = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, cwd=REPO, timeout=30)
        self.assertEqual(out.stdout.strip(), 'True', out.stderr)

    def test_preload_covers_every_module_runtime_and_retry_paths_import(self):
        code = (
            "import os,sys,tempfile\n"
            "os.environ['ORCHD_HOME']=tempfile.mkdtemp()\n"
            "from orchd import app_worker, store\n"
            "app_worker.preload()\n"
            "before={m for m in sys.modules if m.startswith('orchd')}\n"
            "from orchd import core\n"
            "from orchd.runtime import Runtime\n"
            "Runtime()\n"
            "con=store.connect(os.environ['ORCHD_HOME']+'/db')\n"
            "t=store.create_task(con,id='t',repo='r',repo_path='/tmp/r',title='T',instructions='i',done_when='d',"
            "orch_thread='o',codex_bin='codex',model='gpt-6.1-sol',worktree='/tmp/w',status='running',auto_model=1)\n"
            "core.auto_retry_pending(con,store.get_task(con,'t'))\n"
            "print(sorted({m for m in sys.modules if m.startswith('orchd')}-before))\n")
        out = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, cwd=REPO, timeout=30)
        self.assertEqual(out.stdout.strip(), '[]', out.stderr)


LOCK_HOLDER = ("import sys,time\nfrom orchd import store\ncon=store.connect(sys.argv[1])\n"
               "with store.task_delivery(con,[sys.argv[2]],timeout=5):\n    print('held',flush=True)\n    time.sleep(60)\n")


class ControlStaysResponsiveTest(AppBase):
    """Maintenance, reconnect and reconcile failures must never keep the control socket from answering."""

    def setUp(self):
        super().setUp()
        self.servers = []

    def tearDown(self):
        # Stop fixture threads before AppBase removes their database directory.
        for sup, thread in self.servers:
            sup.stopping = True
            thread.join(3)
            self.assertFalse(thread.is_alive())
        super().tearDown()

    def serve(self, setup):
        listener = socket.socket(socket.AF_UNIX)
        self.addCleanup(listener.close)
        listener.bind(str(self.root / 'control.sock'))
        listener.listen(8)
        listener.settimeout(.2)
        ready = threading.Event()
        box = {}

        def run():  # sqlite connections are thread-bound: build the supervisor on the thread that drives it
            sup = box['sup'] = app_worker.Supervisor(self.db, self.id, self.gen)
            sup.rpc = FakeRPC()
            sup.rpc.close = lambda: None
            sup.server = Mock()
            sup.server.poll.return_value = None
            setup(sup)
            ready.set()
            try:
                sup.loop(listener, store.get_task(sup.con, self.id))
            finally:
                sup.con.close()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        ready.wait(5)
        sup = box['sup']
        self.servers.append((sup, thread))
        self.addCleanup(thread.join, 3)
        self.addCleanup(setattr, sup, 'stopping', True)
        return sup

    def health(self):
        started = time.monotonic()
        reply = app_worker.control(store.get_task(self.con, self.id), 'health', timeout=3)
        return reply, time.monotonic() - started

    def test_slow_housekeeping_keeps_health_and_flush_responsive(self):
        entered = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)
        self.queue('retained once')
        def setup(sup):
            def slow():
                entered.set()
                release.wait(8)
                raise TimeoutError('history page stalled')
            sup.collect_quota = slow
        self.serve(setup)
        self.assertTrue(entered.wait(2))
        try:
            reply, elapsed = self.health()
            self.assertLess(elapsed, 3)
            self.assertIsNone(reply['supervisor_error'])
            self.assertTrue(reply['supervisor_busy'])
            with patch.object(app_worker, 'process', return_value=Mock()):
                row = [r for r in core.list_open(self.con, self.rt) if r['task_id'] == self.id][0]
            self.assertIsNone(row['supervisor_error'])
            self.assertNotIn('supervisor busy', row['note'] or '')
            started = time.monotonic()
            result = app_worker.control(store.get_task(self.con, self.id), 'flush', timeout=3)
            self.assertLess(time.monotonic() - started, 3)
            self.assertEqual(result['status'], 'queued')
            self.assertIn('busy', result['reason'])
            self.assertEqual(store.pending_answer_count(self.con, self.id), 1)
        finally:
            release.set()

    def test_queued_during_slow_housekeeping_delivers_without_second_flush(self):
        self.idle()
        self.queue('automatic answer')
        entered = threading.Event()
        finished = threading.Event()
        def setup(sup):
            def slow():
                if not finished.is_set():
                    entered.set()
                    time.sleep(3)
                    finished.set()
            sup.collect_quota = slow
        sup = self.serve(setup)
        self.assertTrue(entered.wait(2))
        result = app_worker.control(self.task, 'flush', timeout=2)
        self.assertEqual(result['status'], 'queued')
        self.assertTrue(finished.wait(4))
        deadline = time.monotonic() + 8
        while store.pending_answer_count(self.con, self.id) and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertEqual(store.pending_answer_count(self.con, self.id), 0)
        self.assertIn('will be delivered automatically', result['reason'])
        time.sleep(.5)
        starts = [p['input'][0]['text'] for m, p in sup.rpc.calls if m == 'turn/start']
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0].count('automatic answer'), 1)

    def test_restart_pending_waits_for_idle_then_delivers_without_flush(self):
        self.queue('survived restart')
        attempted = threading.Event()
        def setup(sup):
            sup.collect_quota = lambda: attempted.set()
        sup = self.serve(setup)
        self.assertTrue(attempted.wait(2))
        time.sleep(.3)
        self.assertFalse([m for m, _ in sup.rpc.calls if m == 'turn/start'])
        self.idle()
        deadline = time.monotonic() + 3
        while store.pending_answer_count(self.con, self.id) and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertEqual(store.pending_answer_count(self.con, self.id), 0)
        time.sleep(.4)
        self.assertEqual(len([m for m, _ in sup.rpc.calls if m == 'turn/start']), 1)

    def test_dead_background_thread_is_visible_over_control_socket(self):
        died = threading.Event()
        def setup(sup):
            def fatal():
                died.set()
                raise SystemExit('fixture background death')
            sup.collect_quota = fatal
        self.serve(setup)
        self.assertTrue(died.wait(2))
        time.sleep(.1)
        reply, _ = self.health()
        self.assertIn('background', reply['supervisor_error'] or '')
        self.assertIn('SystemExit', reply['supervisor_error'])
        self.assertEqual(reply['worker_alive'], 'unknown')

    def test_stale_background_heartbeat_is_visible(self):
        sup = self.supervisor
        sup.background_heartbeat = time.monotonic() - 120
        reply = sup.command({'command': 'health', 'generation': self.gen})
        self.assertIn('heartbeat', reply['supervisor_error'] or '')
        self.assertEqual(reply['worker_alive'], 'unknown')

    def test_restart_recovers_owned_note_but_preserves_close_pending(self):
        sup = self.supervisor
        sup.fail_housekeeping(TimeoutError('old supervisor'))
        owned = store.get_task(self.con, self.id)['note']
        restarted = app_worker.Supervisor(self.db, self.id, self.gen)
        self.addCleanup(restarted.con.close)
        restarted.rpc = FakeRPC()
        restarted.maintain()
        self.assertIsNone(store.get_task(self.con, self.id)['note'])
        for note in ('close-pending: keep me', 'close-pending: ' + owned):
            store.update_task(self.con, self.id, note=note)
            restarted.clear_note()
            self.assertEqual(store.get_task(self.con, self.id)['note'], note)

    def test_restart_preserves_foreign_replacement_of_owned_note(self):
        for foreign in ('close-pending: keep me', 'app-server supervisor: foreign error'):
            with self.subTest(note=foreign):
                store.update_task(self.con, self.id, note=None)
                self.supervisor.fail_housekeeping(TimeoutError('owned'))
                store.update_task(self.con, self.id, note=foreign)
                restarted = app_worker.Supervisor(self.db, self.id, self.gen)
                try:
                    restarted.clear_note()
                finally:
                    restarted.con.close()
                self.assertEqual(store.get_task(self.con, self.id)['note'], foreign)

    def test_error_note_retries_after_foreign_note_is_removed(self):
        sup = self.supervisor
        store.update_task(self.con, self.id, note='close-pending: keep me')
        sup.fail_housekeeping(TimeoutError('still failing'))
        self.assertEqual(store.get_task(self.con, self.id)['note'], 'close-pending: keep me')
        store.update_task(self.con, self.id, note=None)
        sup.fail_housekeeping(TimeoutError('still failing'))
        self.assertIn('still failing', store.get_task(self.con, self.id)['note'] or '')

    def test_changed_error_does_not_replace_foreign_note(self):
        sup = self.supervisor
        for initial in ('close-pending: keep me', None):
            store.update_task(self.con, self.id, note=initial)
            sup.fail_housekeeping(TimeoutError('first'))
            if initial:
                self.assertEqual(store.get_task(self.con, self.id)['note'], initial)
            store.update_task(self.con, self.id, note='close-pending: replacement')
            sup.fail_housekeeping(ImportError('second'))
            self.assertEqual(store.get_task(self.con, self.id)['note'], 'close-pending: replacement')
            self.assertIn('second', sup.maintenance_error)
            sup.clear_note()
            self.assertEqual(store.get_task(self.con, self.id)['note'], 'close-pending: replacement')

    def test_history_page_budget_continues_without_losing_turns(self):
        sup = self.supervisor
        calls = []
        def page(method, params, timeout=20):
            cursor = params['cursor']
            index = int(cursor or 0)
            calls.append(index)
            return {'data': [{'id': str(index), 'status': 'completed'}],
                    'nextCursor': str(index + 1) if index < 10 else None}
        sup.rpc.call = page
        turns = sup.history('thread')
        self.assertIsNone(turns)
        self.assertLessEqual(len(calls), 4)
        for _ in range(10):
            turns = sup.history('thread')
            if turns is not None:
                break
        self.assertEqual([t['id'] for t in turns], [str(i) for i in range(11)])
        self.assertEqual(calls, list(range(11)))

    def test_concurrent_flushes_use_one_delivery_lane(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        self.idle()
        self.queue('one message')
        def setup(sup):
            sup.housekeeping_at = time.monotonic() + 60
            original = sup.rpc.call
            def call(method, params, timeout=20):
                if method == 'turn/start':
                    entered.set()
                    release.wait(5)
                return original(method, params, timeout)
            sup.rpc.call = call
        sup = self.serve(setup)
        replies = []
        client = threading.Thread(target=lambda: replies.append(
            app_worker.control(self.task, 'flush')), daemon=True)
        client.start()
        try:
            self.assertTrue(entered.wait(2))
            busy = core.answer(self.con, self.rt, self.id, 'next message')
            self.assertEqual(busy['status'], 'queued')
            self.assertEqual(busy['pending'], 2)
            reply, elapsed = self.health()
            self.assertLess(elapsed, 3)
        finally:
            release.set()
            client.join(3)
        self.assertFalse(client.is_alive())
        self.assertEqual(replies[0]['delivered'], 1)
        self.assertEqual(store.pending_answer_count(self.con, self.id), 1)
        self.assertEqual(len([m for m, _ in sup.rpc.calls if m == 'turn/start']), 1)
        self.idle()
        result = app_worker.control(self.task, 'flush')
        self.assertEqual(result['delivered'], 1)
        self.assertEqual(store.pending_answer_count(self.con, self.id), 0)
        starts = [p['input'][0]['text'] for m, p in sup.rpc.calls if m == 'turn/start']
        self.assertEqual(sum(t.count('one message') for t in starts), 1)
        self.assertEqual(sum(t.count('next message') for t in starts), 1)

    def test_history_time_budget_retries_only_unconsumed_page(self):
        sup = self.supervisor
        clock = [0.0]
        seen = []
        def page(method, params, timeout=20):
            index = int(params['cursor'] or 0)
            seen.append((index, timeout))
            clock[0] += min(4.8, timeout)
            if timeout < 4.8:
                raise TimeoutError('page budget')
            return {'data': [{'id': str(index)}], 'nextCursor': str(index + 1) if index < 2 else None}
        sup.rpc.call = page
        with patch.object(app_worker.time, 'monotonic', side_effect=lambda: clock[0]):
            with self.assertRaises(TimeoutError):
                sup.history('thread')
            self.assertAlmostEqual(clock[0], 5)
            self.assertAlmostEqual(seen[1][1], .2)
            with self.assertRaises(TimeoutError):
                sup.history('thread')
            turns = sup.history('thread')
        self.assertEqual([t['id'] for t in turns], ['0', '1', '2'])
        self.assertEqual([i for i, _ in seen], [0, 1, 1, 2, 2])

    def test_partial_history_does_not_release_uncertain_delivery_or_flush(self):
        sup = self.supervisor
        mid = self.queue('uncertain answer')
        self.con.execute('INSERT INTO worker_deliveries VALUES(?,?,?,?,?,?,?,?,?)',
                         ('client', self.id, self.gen, 'thread', None, json.dumps([mid]),
                          'uncertain', 'gap', time.time()))
        def page(method, params, timeout=20):
            if method == 'thread/resume':
                return {}
            index = int(params['cursor'] or 0)
            return {'data': [{'id': str(index), 'status': 'completed',
                              'items': [{'clientId': 'client'}] if index == 0 else []}],
                    'nextCursor': str(index + 1) if index < 5 else None}
        sup.rpc.call = page
        self.assertFalse(sup.reconcile())
        self.assertEqual(store.pending_answer_count(self.con, self.id), 1)
        self.assertIsNone(sup.deferred_flush)
        self.assertTrue(sup.reconcile())
        self.assertEqual(store.pending_answer_count(self.con, self.id), 0)
        self.assertEqual(store.get_task(self.con, self.id)['last_completed_turn'], '5')
        self.assertEqual(self.con.execute("SELECT count(*) FROM messages WHERE kind='answer'").fetchone()[0], 1)

    def test_29_second_rpc_sequence_keeps_control_responsive_and_delivers_once(self):
        """Original workload: resume 4.8s, four pages 4.8s, final page times out at 5s.

        The fixture honors reduced per-batch deadlines; timed-out cursors retry next pass.
        All control requests use the real Unix socket and default flush deadline.
        """
        slow_calls = queue.Queue()
        finished = threading.Event()
        starts = []
        timed_out_last = [False]
        self.queue('slow history answer')
        def setup(sup):
            sup.reconcile_pending = True
            sup.collect_quota = lambda: None
            original = sup.rpc.call
            def call(method, params, timeout=20):
                if method not in ('thread/resume', 'thread/turns/list'):
                    if method == 'turn/start':
                        starts.append(params['input'][0]['text'])
                        finished.set()
                    return original(method, params, timeout)
                index = int(params.get('cursor') or 0)
                delay = 5 if method == 'thread/turns/list' and index == 4 else 4.8
                if index == 4 and timed_out_last[0]:
                    return {'data': [{'id': 'last', 'status': 'completed'}], 'nextCursor': None}
                slow_calls.put(method)
                time.sleep(min(delay, timeout))
                if timeout < delay or (method == 'thread/turns/list' and index == 4):
                    if index == 4 and timeout > 4.9:
                        timed_out_last[0] = True
                    raise TimeoutError('history page stalled')
                if method == 'thread/resume':
                    return {}
                return {'data': [{'id': str(index), 'status': 'completed'}], 'nextCursor': str(index + 1)}
            sup.rpc.call = call
        sup = self.serve(setup)
        health_times, flush_times = [], []
        deadline = time.monotonic() + 50
        while not finished.is_set() and time.monotonic() < deadline:
            try:
                slow_calls.get(timeout=.2)
            except queue.Empty:
                continue
            reply, elapsed = self.health()
            health_times.append(elapsed)
            self.assertTrue(reply['supervisor_busy'])
            started = time.monotonic()
            result = app_worker.control(store.get_task(self.con, self.id), 'flush')
            flush_times.append(time.monotonic() - started)
            self.assertEqual(result['status'], 'queued')
            self.assertIn('retained', result['reason'])
            self.assertEqual(store.pending_answer_count(self.con, self.id), 1)
        self.assertTrue(finished.wait(2))
        # Wait for the receipt, not merely the turn/start bytes.
        deadline = time.monotonic() + 2
        while store.pending_answer_count(self.con, self.id) and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertEqual(store.pending_answer_count(self.con, self.id), 0)
        app_worker.control(store.get_task(self.con, self.id), 'flush')
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0].count('slow history answer'), 1)
        self.assertLess(max(health_times), 3)
        self.assertLess(max(flush_times), 25)
        print(f'29s workload: health max={max(health_times):.6f}s; flush max={max(flush_times):.6f}s; '
              f'probes={len(health_times)}; delivered={len(starts)}', flush=True)

    def test_consecutive_timeouts_answer_health_without_busy_loop(self):
        sup = self.serve(lambda s: setattr(s, 'collect_quota', Mock(side_effect=TimeoutError('delivery busy'))))
        cpu = time.process_time()
        time.sleep(.75)
        reply, elapsed = self.health()
        self.assertLess(time.process_time() - cpu, .3)
        self.assertLess(elapsed, 1)
        self.assertIn('TimeoutError', reply['supervisor_error'])
        self.assertIn('TimeoutError', store.get_task(self.con, self.id)['note'])
        self.assertLess(sup.collect_quota.call_count, 10)  # backs off instead of retrying every pass

    def test_deferred_flush_timeout_is_reported_not_swallowed(self):
        def setup(sup):
            sup.deferred_flush = (self.gen, 'thread', 'turn')
            sup.flush = Mock(side_effect=TimeoutError('task delivery busy'))
        self.serve(setup)
        time.sleep(.3)
        reply, _ = self.health()
        self.assertIn('task delivery busy', reply['supervisor_error'])

    def test_failing_reconnect_still_answers_control(self):
        with patch.object(app_worker, 'RPC', side_effect=ConnectionRefusedError('no socket')):
            self.serve(lambda s: setattr(s.rpc, 'error', ConnectionError('gone')))
            time.sleep(.5)
            reply, elapsed = self.health()
        self.assertLess(elapsed, 1)
        self.assertIn('ConnectionRefusedError', reply['supervisor_error'])

    def test_failing_reconcile_still_answers_control(self):
        self.serve(lambda s: (setattr(s, 'reconcile_pending', True),
                              setattr(s, 'reconcile', Mock(side_effect=TimeoutError('slow')))))
        time.sleep(.5)
        reply, elapsed = self.health()
        self.assertLess(elapsed, 1)
        self.assertIn('TimeoutError', reply['supervisor_error'])

    def test_recovery_clears_only_its_own_note(self):
        sup = self.supervisor
        with patch.object(sup, 'collect_quota', side_effect=[TimeoutError('busy'), None, None]):
            sup.maintain()
            sup.maintain()
        self.assertIsNone(store.get_task(self.con, self.id)['note'])
        self.assertIsNone(sup.maintenance_error)
        with patch.object(sup, 'collect_quota', side_effect=[TimeoutError('busy'), None]):
            sup.maintain()
            store.update_task(self.con, self.id, note='app-server delivery rejected; pending retained: x')
            sup.maintain()
        self.assertIn('delivery rejected', store.get_task(self.con, self.id)['note'])

    def test_health_replies_when_rpc_is_down(self):
        self.serve(lambda s: setattr(s.rpc, 'fail', TimeoutError('rpc stuck')))
        reply, _ = self.health()
        self.assertEqual(reply['worker_alive'], 'unknown')
        self.assertIn('rpc stuck', reply['supervisor_error'])

    def test_cached_rpc_error_is_not_cleared_before_successful_refresh(self):
        sup = self.supervisor
        sup.rpc.fail = TimeoutError('still down')
        sup.maintain()
        sup.maintain()  # cached health refresh is not due yet
        self.assertIn('still down', sup.maintenance_error)
        self.assertIn('still down', store.get_task(self.con, self.id)['note'])
        sup.rpc.fail = None
        sup.health_refresh_at = 0
        sup.maintain()
        self.assertIsNone(sup.maintenance_error)
        self.assertIsNone(store.get_task(self.con, self.id)['note'])

    def test_supervisor_error_reaches_health_detail_and_list_open(self):
        task = store.get_task(self.con, self.id)
        with patch.object(app_worker, 'process', return_value=Mock()), \
                patch.object(app_worker, 'control',
                             return_value=dict(worker_alive='idle', supervisor_error='TimeoutError: busy')):
            self.assertEqual(app_worker.health(task), 'idle')  # old callers still get the plain string
            self.assertEqual(app_worker.health_detail(task), ('idle', 'TimeoutError: busy'))
            row = [r for r in core.list_open(self.con, self.rt) if r['task_id'] == self.id][0]
        self.assertEqual(row['supervisor_error'], 'TimeoutError: busy')
        self.assertIn('TimeoutError: busy', row['note'])
        self.assertEqual(row['worker_health'], 'alive')


class RealLockHolderTest(IsolationBase):
    def test_held_delivery_lock_does_not_hide_supervisor_from_health(self):
        holder = subprocess.Popen([sys.executable, '-c', LOCK_HOLDER, str(self.db), self.id], stdout=subprocess.PIPE,
                                  text=True, cwd=REPO)
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        self.assertEqual(holder.stdout.readline().strip(), 'held')
        task = app_worker.start(self.con, self.rt, store.get_task(self.con, self.id), 'first prompt', 'gpt-6.1-sol')
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and 'TimeoutError' not in (store.get_task(self.con, self.id)['note'] or ''):
            time.sleep(.1)
        started = time.monotonic()
        alive, error = app_worker.health_detail(task)
        self.assertLess(time.monotonic() - started, 3)
        self.assertIn(alive, ('idle', 'active'))
        self.assertIn('TimeoutError', error)
        self.assertIn('TimeoutError', store.get_task(self.con, self.id)['note'])
        holder.kill()
        holder.wait()
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and app_worker.health_detail(task)[1]:
            time.sleep(.2)
        self.assertIsNone(app_worker.health_detail(task)[1])  # recovers once the lock is free


if __name__ == '__main__':
    unittest.main()
