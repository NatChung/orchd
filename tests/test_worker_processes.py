"""Real detached background processes through close; no vendor CLI or user processes."""
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from orchd import core, store
from orchd.runtime import Runtime


class WorkerProcessesTest(unittest.TestCase):
    def test_failed_cleanup_keeps_task_open_and_worktree_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = store.connect(Path(tmp) / 'tasks.db')
            self.addCleanup(con.close)
            store.create_task(con, id='t1', repo='demo', repo_path=tmp, title='T', instructions='i', done_when='d',
                              orch_thread='th', codex_bin='c', model='gpt-6.1-sol', worktree=tmp, status='running')
            rt = Runtime()
            with mock.patch('orchd.runtime.cleanup_processes', side_effect=RuntimeError('task processes still alive')), \
                 mock.patch.object(rt, 'remove_worktree') as remove:
                with self.assertRaisesRegex(RuntimeError, 'task processes still alive'):
                    core.close(con, rt, 't1')
            remove.assert_not_called()
            self.assertEqual(store.get_task(con, 't1')['status'], 'running')
            self.assertEqual(con.execute("SELECT count(*) FROM messages WHERE kind='close'").fetchone()[0], 0)

    def test_close_escalates_detached_children_and_preserves_unrelated_processes(self):
        self.exercise(ignore_term=True)

    def test_close_terminates_cooperative_detached_children(self):
        self.exercise(ignore_term=False)

    def test_close_cleans_orphans_after_worker_already_exited(self):
        self.exercise(ignore_term=True, exited=True)

    def exercise(self, ignore_term, exited=False):
        for kind, model in (("claude", "claude-sonnet-5-5"), ("codex", "gpt-6.1-sol")):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                wt, other_wt = root / "task", root / "other"
                wt.mkdir()
                other_wt.mkdir()
                ready = root / "ready"
                # Child escapes both the worker's group and session, then changes cwd.
                child = ("import os,pathlib,signal,time; os.chdir('/'); "
                         + ("signal.signal(signal.SIGTERM,signal.SIG_IGN); " if ignore_term else "")
                         + f"pathlib.Path({str(ready)!r}).write_text(str(os.getpid())); time.sleep(300)")
                script = ("import subprocess,sys,time; "
                          f"p=subprocess.Popen([sys.executable,'-c',{child!r}],start_new_session=True); "
                          + ("sys.exit(0)" if exited else "time.sleep(300)"))
                env = dict(os.environ, ORCHD_WORKTREE=str(wt.resolve()))
                worker = subprocess.Popen([sys.executable, "-c", script, str(ready), "codex", str(wt)],
                                          cwd=wt, env=env, start_new_session=True)
                other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"],
                                         cwd=other_wt, env=dict(os.environ, ORCHD_WORKTREE=str(other_wt.resolve())),
                                         start_new_session=True)
                # Even a user's unmarked process in our worktree must survive.
                user_env = dict(os.environ)
                user_env.pop("ORCHD_WORKTREE", None)
                user = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"], cwd=wt, env=user_env)
                pid = None
                try:
                    deadline = time.monotonic() + 5
                    while not ready.exists() and time.monotonic() < deadline:
                        time.sleep(.02)
                    self.assertTrue(ready.exists())
                    pid = int(ready.read_text())
                    self.assertEqual(os.getpgid(pid), pid)
                    if exited:
                        worker.wait(timeout=5)
                    else:
                        self.assertNotEqual(os.getpgid(pid), os.getpgid(worker.pid))
                    con = store.connect(root / "tasks.db")
                    self.addCleanup(con.close)
                    store.create_task(con, id="t1", repo="demo", repo_path=str(root), title="T", instructions="i",
                                      done_when="d", orch_thread="th", codex_bin="c", model=model,
                                      worktree=str(wt), job_id=None if exited else str(worker.pid),
                                      session_id="thread", status="running")
                    rt = Runtime()
                    def run(cmd, **kwargs):
                        if cmd[:2] == [rt.claude, "stop"]:
                            if worker.poll() is None:
                                worker.terminate()
                                worker.wait(timeout=5)
                            return subprocess.CompletedProcess(cmd, 0, "", "")
                        return Runtime.run(rt, cmd, **kwargs)
                    with mock.patch.object(rt, "run", side_effect=run), \
                         mock.patch.object(rt, "live_jobs", return_value={}), \
                         mock.patch.object(rt, "worktree_state", return_value=(False, "test kept")), \
                         mock.patch.object(rt, "claude_usage", return_value=None), \
                         mock.patch.object(rt, "codex_usage", return_value=None):
                        result = core.close(con, rt, "t1")
                    self.assertFalse(rt.pid_alive(pid), "detached child survived close")
                    self.assertIsNone(other.poll(), "another worker was killed")
                    self.assertIsNone(user.poll(), "user's unmarked process was killed")
                    self.assertIn(pid, result["process_cleanup"]["terminated"])
                    self.assertEqual(pid in result["process_cleanup"]["killed"], ignore_term)
                    event = json.loads(con.execute("SELECT body FROM messages WHERE kind='close'").fetchone()[0])
                    self.assertEqual(event["process_cleanup"], result["process_cleanup"])
                finally:
                    for proc in (worker, other, user):
                        if proc.poll() is None:
                            proc.kill()
                        proc.wait()
                    if pid:
                        try:
                            os.kill(pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass


class AppProcessIdentityTest(unittest.TestCase):
    def test_reused_birth_is_not_signalled_and_bounded_kill_targets_only_group(self):
        import json
        import subprocess
        import sys
        from orchd import app_worker
        unrelated=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],start_new_session=True)
        owned=subprocess.Popen([sys.executable,'-c',
            'import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print("ready",flush=True); time.sleep(60)'],
            stdout=subprocess.PIPE,text=True,start_new_session=True)
        try:
            self.assertEqual(owned.stdout.readline().strip(),'ready')
            raw=app_worker.identity(owned.pid)
            wrong=json.loads(raw);wrong['birth']-=1
            app_worker.stop_identity(json.dumps(wrong),grace=.1)
            self.assertIsNone(owned.poll())
            app_worker.stop_identity(raw,grace=.1)
            self.assertEqual(owned.wait(timeout=2),-9)
            self.assertIsNone(unrelated.poll())
        finally:
            for p in (owned,unrelated):
                if p.poll() is None:p.kill()
                p.wait()
            owned.stdout.close()
