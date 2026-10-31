"""Issue #107: the app-server supervisor and viewer must never import orchd from the worker's worktree."""
import json
import os
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
        self.assertEqual(result['status'], 'delivered')
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
    def test_answer_reports_supervisor_note_when_control_times_out(self):
        store.update_task(self.con, self.id, note='app-server supervisor: ImportError: cannot import name X')
        with patch.object(app_worker, 'control', side_effect=TimeoutError('timed out')):
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
            sup.loop(listener, store.get_task(sup.con, self.id))

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        ready.wait(5)
        sup = box['sup']
        self.addCleanup(thread.join, 3)
        self.addCleanup(setattr, sup, 'stopping', True)
        return sup

    def health(self):
        started = time.monotonic()
        reply = app_worker.control(store.get_task(self.con, self.id), 'health', timeout=3)
        return reply, time.monotonic() - started

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
