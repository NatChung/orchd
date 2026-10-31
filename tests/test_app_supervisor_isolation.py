"""Issue #107: the app-server supervisor and viewer must never import orchd from the worker's worktree."""
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from orchd import app_worker, core, store
from orchd.runtime import Runtime
from tests.app_support import AppBase

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


if __name__ == '__main__':
    unittest.main()
