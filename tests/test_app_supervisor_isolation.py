"""Issue #107: the app-server supervisor and viewer must never import orchd from the worker's worktree."""
import json
import os
import shutil
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
        self.children = []
        popen = subprocess.Popen
        def tracked_popen(*args, **kwargs):
            child = popen(*args, **kwargs)
            self.children.append(child)
            return child
        launcher = patch.object(subprocess, 'Popen', side_effect=tracked_popen)
        launcher.start()
        self.addCleanup(launcher.stop)
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
        for child in self.children:
            child.wait(timeout=5)
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

    def isolated_package(self):
        package = self.root / 'installed' / 'orchd'
        shutil.copytree(REPO / 'orchd', package, ignore=shutil.ignore_patterns('__pycache__'))
        return package

    def test_disk_edits_after_preload_do_not_break_delivery(self):
        package = self.isolated_package()
        with patch.object(app_worker, '__file__', str(package / 'app_worker.py')):
            app_worker.start(self.con, self.rt, store.get_task(self.con, self.id), 'first', 'gpt-6.1-sol')
        for module in ('config', 'runtime', 'core'):
            (package / (module + '.py')).write_text("raise ImportError('EDITED-MODULE-MUST-NOT-LOAD')\n")
        deadline = time.monotonic() + 5
        while store.get_task(self.con, self.id)['turn_state'] != 'completed' and time.monotonic() < deadline:
            time.sleep(.05)
        store.add_message(self.con, self.id, store.QUEUED, 'after disk edit')
        result = app_worker.control(store.get_task(self.con, self.id), 'flush', timeout=10)
        self.assertEqual(result['status'], 'delivered')
        self.assertIn('after disk edit', [e['text'] for e in self.logged() if e['event'] == 'turn/start'][-1])

    def test_preload_failure_reports_module_and_cause(self):
        package = self.isolated_package()
        (package / 'config.py').write_text("raise ImportError('BROKEN-CONFIG-FIXTURE')\n")
        with patch.object(app_worker, '__file__', str(package / 'app_worker.py')):
            with self.assertRaisesRegex(RuntimeError, 'preload failed: ImportError: BROKEN-CONFIG-FIXTURE'):
                app_worker.start(self.con, self.rt, store.get_task(self.con, self.id), 'first', 'gpt-6.1-sol')
        self.assertEqual(self.logged(), [])

    def test_startup_failure_reports_original_cause(self):
        self.fake.write_text('#!/bin/sh\necho FIXTURE-SERVER-FAILED >&2\nexit 7\n')
        with self.assertRaisesRegex(RuntimeError, 'app-server startup failed:.*') as caught:
            app_worker.start(self.con, self.rt, store.get_task(self.con, self.id), 'first', 'gpt-6.1-sol')
        self.assertIn('FileNotFoundError', str(caught.exception))

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


class PreloadTest(AppBase):
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
