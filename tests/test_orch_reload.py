"""Reload changes only the Orch process, with durable uncertainty and no author moves."""
import json
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from orchd import orch_reload, orch_revive, store
from orchd.runtime import Runtime


class FakeRuntime:
    def __init__(self, home):
        self.home = home
        self.jobs = {"old-job": dict(id="old-job", sessionId="original-session", cwd=str(home),
                                    pid=100, status="idle", state="blocked")}
        self.starts = []
        self.stops = []
        self.mode = "normal"

    def live_jobs(self):
        return self.jobs

    def socket_listening(self, sock):
        return any(self.jobs.get(key, {}).get("pid") for key in ("old-job", "new-job"))

    def stop_worker(self, job):
        self.stops.append(job)
        self.jobs[job].update(pid=None, status=None, state="failed")
        if self.mode == "crash-stop":
            self.mode = "normal"
            raise KeyboardInterrupt()

    def orch_socket_path(self, orch_id):
        return "/tmp/example-orch.sock"

    def start_orch(self, orch_id, model, home, resume=None):
        self.starts.append((orch_id, model, home, resume))
        session = "wrong-session" if self.mode == "wrong-session" else resume
        self.jobs["new-job"] = dict(id="new-job", sessionId=session, cwd=str(home),
                                    pid=101, status="idle", state="blocked")
        if self.mode == "unknown":
            raise TimeoutError()
        if self.mode == "crash-spawn":
            raise KeyboardInterrupt()
        return self.orch_socket_path(orch_id), "new-job", session


class ReloadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name).resolve()
        self.con = store.connect(self.home / "state.db")
        self.con.execute("INSERT INTO orchs(id,kind,model,socket,session_id,job_id,created_at) "
                         "VALUES('owner','claude','example-model','/tmp/example-orch.sock',"
                         "'original-session','old-job',1)")
        self.con.execute("INSERT INTO tasks(id,repo,repo_path,title,instructions,done_when,model,model_reason,"
                         "task_type,orch_thread,session_id,generation,created_at,updated_at,codex_bin,status) "
                         "VALUES('author','demo','/srv/demo','T','i','d','example-worker','r','outward',"
                         "'owner','author-original',7,1,1,'/usr/bin/example','running')")
        self.before = [tuple(r) for r in self.con.execute("SELECT * FROM tasks")]
        self.rt = FakeRuntime(self.home)
        self.op = str(uuid.uuid4())
        self.env = mock.patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.paths = mock.patch.object(orch_reload.paths, "orch_home", return_value=self.home)
        self.paths.start()

    def tearDown(self):
        self.paths.stop()
        self.env.stop()
        self.assertEqual(self.before, [tuple(r) for r in self.con.execute("SELECT * FROM tasks")])
        self.con.close()
        self.tmp.cleanup()

    def reload(self, **kw):
        return orch_reload.reload(self.con, self.rt, "owner", self.op, "original-session", "old-job",
                                  kw.get("model", "example-model"))

    def test_complete_and_repeat_preserve_identity_and_all_author_bytes(self):
        self.assertEqual(self.reload()["phase"], "complete")
        self.assertEqual(self.reload()["phase"], "complete")
        row = store.get_orch(self.con, "owner")
        self.assertEqual((row["id"], row["session_id"], row["model"], row["job_id"], row["stopped_at"]),
                         ("owner", "original-session", "example-model", "new-job", None))
        self.assertEqual(len(self.rt.starts), 1)
        self.assertEqual(self.rt.stops, ["old-job"])

    def test_crash_after_stop_resumes_safely_once(self):
        self.rt.mode = "crash-stop"
        with self.assertRaises(KeyboardInterrupt):
            self.reload()
        self.assertEqual(orch_reload.status(self.con, self.op)["phase"], "stop_pending")
        self.assertEqual(self.reload()["phase"], "complete")
        self.assertEqual(len(self.rt.starts), 1)

    def test_unknown_spawn_and_repeat_never_start_second_and_blocks_old_revive(self):
        self.rt.mode = "unknown"
        self.assertEqual(self.reload()["phase"], "spawn_pending")
        self.assertEqual(self.reload()["phase"], "spawn_pending")
        with self.assertRaisesRegex(ValueError, "orch stop"):
            orch_revive.revive(self.con, self.rt, "owner", seen_job="old-job")
        self.assertEqual(len(self.rt.starts), 1)
        self.assertEqual(orch_reload.reconcile(self.con, self.rt, self.op)["phase"], "spawn_pending")
        self.assertEqual(orch_reload.reconcile(self.con, self.rt, self.op, "new-job")["phase"], "complete")
        self.assertEqual(len(self.rt.starts), 1)

    def test_process_crash_during_spawn_is_durable_unknown(self):
        self.rt.mode = "crash-spawn"
        with self.assertRaises(KeyboardInterrupt):
            self.reload()
        self.assertEqual(self.reload()["phase"], "spawn_pending")
        self.assertEqual(len(self.rt.starts), 1)

    def test_wrong_returned_session_stays_quiesced_without_stopping_unproven_job(self):
        self.rt.mode = "wrong-session"
        result = self.reload()
        self.assertEqual((result["phase"], result["error"]), ("held", "returned_session_mismatch"))
        self.assertEqual(self.rt.stops, ["old-job"])
        self.assertIsNotNone(store.get_orch(self.con, "owner")["stopped_at"])
        self.reload()
        self.assertEqual(len(self.rt.starts), 1)

    def test_wrong_returned_existing_author_job_is_never_stopped(self):
        self.rt.jobs["author-job"] = dict(id="author-job", sessionId="author-original", cwd="/srv/author",
                                          pid=200, status="idle", state="blocked")
        with mock.patch.object(self.rt, "start_orch", return_value=("/tmp/new.sock", "author-job", "wrong")):
            self.assertEqual(self.reload()["phase"], "held")
        self.assertEqual(self.rt.stops, ["old-job"])
        self.assertEqual(self.rt.jobs["author-job"]["pid"], 200)

    def test_wrong_receipt_cannot_stop_an_author_created_after_baseline(self):
        def wrong_provider(*args, **kwargs):
            self.rt.starts.append((args, kwargs))
            self.rt.jobs['concurrent-author'] = dict(id='concurrent-author', sessionId='other-author',
                cwd='/srv/another-author', pid=300, status='idle', state='blocked')
            return '/tmp/another-author.sock', 'concurrent-author', 'other-author'
        with mock.patch.object(self.rt, 'start_orch', side_effect=wrong_provider):
            result = self.reload()
        self.assertEqual(result['phase'], 'held')
        self.assertEqual(self.rt.stops, ['old-job'])
        self.assertEqual(self.rt.jobs['concurrent-author']['pid'], 300)
        self.assertEqual(self.reload()['phase'], 'held')
        self.assertEqual(len(self.rt.starts), 1)

    def test_mismatch_and_worker_entry_rejected_before_stop(self):
        with self.assertRaises(ValueError):
            self.reload(model="another-model")
        with mock.patch.dict(os.environ, {"ORCHD_ORCH_ID": "owner"}):
            with self.assertRaises(PermissionError):
                self.reload()
        self.assertFalse(self.rt.stops)

    def test_concurrent_human_stop_marker_wins(self):
        original = self.rt.start_orch
        def start(*args, **kw):
            result = original(*args, **kw)
            self.con.execute("UPDATE orchs SET stopped_at=123 WHERE id='owner'")
            return result
        with mock.patch.object(self.rt, "start_orch", side_effect=start):
            with self.assertRaises(ValueError):
                self.reload()
        self.assertEqual(store.get_orch(self.con, "owner")["stopped_at"], 123)

    def test_stop_transport_failure_is_explicit_and_recoverable(self):
        with mock.patch.object(self.rt, "stop_worker", side_effect=TimeoutError()):
            row = self.reload()
        self.assertEqual((row["phase"], row["error"]), ("stop_pending", "old_stop_confirmation_unknown"))
        self.assertFalse(self.rt.starts)
        self.assertEqual(self.reload()["phase"], "complete")

    def test_new_job_probe_failure_retains_receipt_without_second_spawn(self):
        original = self.rt.start_orch
        def start(*args, **kw):
            result = original(*args, **kw)
            self.rt.live_jobs = mock.Mock(side_effect=TimeoutError())
            return result
        with mock.patch.object(self.rt, "start_orch", side_effect=start):
            row = self.reload()
        self.assertEqual((row["phase"], row["error"]), ("receipt", "new_job_probe_unknown"))
        self.assertEqual(self.reload()["phase"], "receipt")
        self.rt.live_jobs = lambda: self.rt.jobs
        self.assertEqual(orch_reload.reconcile(self.con, self.rt, self.op)["phase"], "complete")
        self.assertEqual(len(self.rt.starts), 1)

    def test_absent_old_job_is_not_positive_stop_confirmation(self):
        def stop(job):
            self.rt.jobs.pop(job)
        with mock.patch.object(self.rt, "stop_worker", side_effect=stop):
            self.assertEqual(self.reload()["phase"], "stop_pending")
        self.assertFalse(self.rt.starts)

    def test_reconcile_rejects_ambiguous_native_candidates(self):
        self.rt.mode = "unknown"
        self.reload()
        self.rt.jobs["other-job"] = dict(self.rt.jobs["new-job"], id="other-job")
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            orch_reload.reconcile(self.con, self.rt, self.op, "new-job")

    def test_native_start_writes_mcp_with_current_deployment_python(self):
        sock = str(self.home / "o.sock")
        with mock.patch.object(Runtime, "orch_socket_path", return_value=sock), \
                mock.patch.object(Runtime, "start_claude", return_value=("new", "original-session")):
            Runtime().start_orch("owner", "example-model", self.home, resume="original-session")
        config = json.loads((self.home / "mcp.json").read_text())
        self.assertEqual(config["mcpServers"]["orchd"]["command"], sys.executable)


if __name__ == "__main__":
    unittest.main()
