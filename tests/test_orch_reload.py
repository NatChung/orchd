"""Reload changes only the Orch process, with durable uncertainty and no author moves."""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

import psutil
from unittest import mock

from orchd import orch_reload, orch_revive, store
from orchd.runtime import Runtime, mcp_env


class FakeRuntime:
    def __init__(self, home):
        self.home = home
        self.claude = "/srv/bin/example-claude"
        self.jobs = {"old-job": dict(id="old-job", sessionId="original-session", cwd=str(home),
                                    pid=100, startedAt=1000000, status="idle", state="blocked")}
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
        self.con = store.connect(self.home / "orchd.db")
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
        self.env = mock.patch.dict(os.environ, {"ORCHD_HOME": str(self.home)}, clear=True)
        self.env.start()
        self.paths = mock.patch.object(orch_reload.paths, "orch_home", return_value=self.home)
        self.paths.start()
        self.birth = mock.patch.object(orch_reload, "_process_birth", return_value=1000.0)
        self.birth.start()

    def tearDown(self):
        self.birth.stop()
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

    def removed_job(self, mode="normal"):
        def stop(job):
            self.rt.stops.append(job)
            self.rt.jobs.pop(job, None)
        self.rt.mode = mode
        return mock.patch.object(self.rt, "stop_worker", side_effect=stop)

    def test_removed_provider_job_requires_positive_pid_absence(self):
        with self.removed_job(), mock.patch.object(orch_reload, "_process_birth",
                side_effect=[1000.0, psutil.NoSuchProcess(100)]):
            result = self.reload()
        self.assertEqual(result["phase"], "complete")
        self.assertEqual(result["stop_evidence"]["kind"], "original_pid_absent")
        self.assertEqual(result["preflight"]["process_birth"], 1000.0)
        self.assertEqual(len(self.rt.starts), 1)

    def test_exact_saved_birth_can_prove_pid_reuse(self):
        with self.removed_job(), mock.patch.object(orch_reload, "_process_birth", side_effect=[1000.0, 2000.0]):
            result = self.reload()
        self.assertEqual(result["phase"], "complete")
        self.assertEqual(result["stop_evidence"]["kind"], "original_pid_reused")

    def test_process_unreadable_or_same_birth_cannot_confirm_removed_job(self):
        saved = {"provider": orch_reload._provider(self.rt), "job": dict(self.rt.jobs["old-job"]),
                 "process_birth": 1000.0}
        row = dict(old_job="old-job", session_id="original-session", home=str(self.home), preflight=saved)
        for probe in (1000.0, psutil.AccessDenied(100)):
            with self.subTest(probe=probe), mock.patch.object(orch_reload, "_process_birth", side_effect=[probe]):
                self.assertIsNone(orch_reload._stop_proof(row, self.rt, {}, False))

    def test_provider_unknown_or_original_session_survivor_blocks_stop(self):
        for jobs in (None, {"survivor": dict(id="survivor", sessionId="original-session", pid=999,
                                            status="idle", cwd=str(self.home))}):
            with self.subTest(jobs=jobs):
                saved = {"provider": orch_reload._provider(self.rt), "job": dict(self.rt.jobs["old-job"]),
                         "process_birth": 1000.0}
                row = dict(old_job="old-job", session_id="original-session", home=str(self.home), preflight=saved)
                with mock.patch.object(orch_reload, "_process_birth", side_effect=psutil.NoSuchProcess(100)):
                    self.assertIsNone(orch_reload._stop_proof(row, self.rt, jobs, False))
                    self.assertIsNone(orch_reload._stop_proof(row, self.rt, {}, None))

    def test_unidentified_live_jobs_block_positive_stop_confirmation(self):
        saved = {"provider": orch_reload._provider(self.rt), "job": dict(self.rt.jobs["old-job"]),
                 "process_birth": 1000.0}
        row = dict(old_job="old-job", session_id="original-session", home=str(self.home), preflight=saved)
        for session in (None, "", "   ", 42):
            with self.subTest(session=session), mock.patch.object(orch_reload, "_process_birth",
                    side_effect=psutil.NoSuchProcess(100)):
                jobs = {"unidentified": dict(pid=999, status="idle", sessionId=session)}
                self.assertIsNone(orch_reload._stop_proof(row, self.rt, jobs, False))

    def test_retired_old_row_does_not_hide_another_live_original_session(self):
        saved = {"provider": orch_reload._provider(self.rt), "job": dict(self.rt.jobs["old-job"]),
                 "process_birth": 1000.0}
        row = dict(old_job="old-job", session_id="original-session", home=str(self.home), preflight=saved)
        jobs = {"old-job": dict(pid=None, status=None),
                "survivor": dict(pid=999, status="idle", sessionId="original-session")}
        self.assertIsNone(orch_reload._stop_proof(row, self.rt, jobs, False))

    def test_another_provider_profile_cannot_confirm_stop(self):
        saved = {"provider": dict(command="/srv/other-claude", config="/srv/other-profile"),
                 "job": dict(self.rt.jobs["old-job"]), "process_birth": 1000.0}
        row = dict(old_job="old-job", session_id="original-session", home=str(self.home), preflight=saved)
        with mock.patch.object(orch_reload, "_process_birth", side_effect=psutil.NoSuchProcess(100)):
            self.assertIsNone(orch_reload._stop_proof(row, self.rt, {}, False))

    def legacy_preflight(self):
        snapshot = {"orch": dict(store.get_orch(self.con, "owner")), "job": dict(self.rt.jobs["old-job"])}
        path = self.home / "preflight.json"
        path.write_text(json.dumps(snapshot))
        path.chmod(0o600)
        os.utime(path, (1, 1))
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        with mock.patch.object(self.rt, "stop_worker", side_effect=TimeoutError()):
            self.reload()
        # Emulate the already-deployed schema's operation without a descriptor.
        self.con.execute("UPDATE orch_reload_operations SET preflight=NULL WHERE operation_id=?", (self.op,))
        return path, digest

    def test_protected_legacy_attachment_then_pid_absent_continues_same_op_once(self):
        path, digest = self.legacy_preflight()
        attached = orch_reload.attach_stop_preflight(self.con, self.rt, self.op, path, digest)
        self.assertIsNone(attached["preflight"]["process_birth"])
        self.assertEqual(attached["preflight"]["source_sha256"], digest)
        self.assertFalse(self.rt.starts)
        orch_reload.attach_stop_preflight(self.con, self.rt, self.op, path, digest)
        with self.removed_job("unknown"), mock.patch.object(orch_reload, "_process_birth",
                side_effect=psutil.NoSuchProcess(100)):
            self.assertEqual(self.reload()["phase"], "spawn_pending")
            self.assertEqual(self.reload()["phase"], "spawn_pending")
        self.assertEqual(len(self.rt.starts), 1)

    def test_legacy_birth_cannot_be_inferred_from_provider_started_at(self):
        path, digest = self.legacy_preflight()
        orch_reload.attach_stop_preflight(self.con, self.rt, self.op, path, digest)
        with self.removed_job(), mock.patch.object(orch_reload, "_process_birth", return_value=2000.0):
            self.assertEqual(self.reload()["phase"], "stop_pending")
        self.assertFalse(self.rt.starts)

    def test_attachment_refuses_wrong_digest_identity_new_file_and_public_writes(self):
        path, digest = self.legacy_preflight()
        with self.assertRaises(ValueError):
            orch_reload.attach_stop_preflight(self.con, self.rt, self.op, path, "0" * 64)
        path.chmod(0o666)
        with self.assertRaises(ValueError):
            orch_reload.attach_stop_preflight(self.con, self.rt, self.op, path, digest)
        path.chmod(0o600)
        os.utime(path, None)
        with self.assertRaises(ValueError):
            orch_reload.attach_stop_preflight(self.con, self.rt, self.op, path, digest)
        os.utime(path, (1, 1))
        data = json.loads(path.read_text())
        data["job"]["sessionId"] = "another-session"
        path.write_text(json.dumps(data))
        os.utime(path, (1, 1))
        with self.assertRaises(ValueError):
            orch_reload.attach_stop_preflight(self.con, self.rt, self.op, path,
                                             hashlib.sha256(path.read_bytes()).hexdigest())

    def test_reconcile_rejects_ambiguous_native_candidates(self):
        self.rt.mode = "unknown"
        self.reload()
        self.rt.jobs["other-job"] = dict(self.rt.jobs["new-job"], id="other-job")
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            orch_reload.reconcile(self.con, self.rt, self.op, "new-job")

    def copy_evidence(self):
        sock = str(self.home / "o.sock")
        self.con.execute("UPDATE orchs SET socket=? WHERE id='owner'", (sock,))
        self.rt.orch_socket_path = lambda orch: sock
        self.rt.mode = "wrong-session"
        result = self.reload()
        mcp = self.home / "mcp.json"
        mcp.write_text(json.dumps({"mcpServers": {"orchd": {"command": sys.executable,
                "args": [orch_reload.paths.orchd_executable(), "mcp"], "env": mcp_env("owner")}}}))
        mcp.chmod(0o600)
        launch = {"mode": "resume", "sessionId": "original-session", "fork": True,
                  "restoresTranscript": True, "flagArgs": ["--model", "example-model", "--mcp-config", str(mcp),
                    "--messaging-socket-path", sock, "--strict-mcp-config"]}
        roster = {"proto": 1, "workers": {"new-job": {"pid": 101, "sessionId": "wrong-session",
                    "cwd": str(self.home), "dispatch": {"proto": 1, "short": "new-job",
                    "sessionId": "wrong-session", "createdAt": time.time() * 1000, "cwd": str(self.home),
                    "launch": launch}}}}
        path = self.home / "roster.json"
        self.save_roster(path, roster)
        return path, roster

    def save_roster(self, path, roster):
        path.write_text(json.dumps(roster))
        path.chmod(0o600)
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def accept_copy(self, path, digest=None, job="new-job", session="wrong-session"):
        return orch_reload.accept_resume_copy(self.con, self.rt, self.op, job, session, path,
                                              digest or hashlib.sha256(path.read_bytes()).hexdigest())

    def test_accept_copy_changes_only_controller_and_retains_original_operation_session(self):
        path, roster = self.copy_evidence()
        result = self.accept_copy(path)
        self.assertEqual(result["phase"], "complete")
        self.assertEqual(result["session_id"], "original-session")
        self.assertEqual(result["lineage"]["original_session"], "original-session")
        self.assertEqual(result["lineage"]["accepted_session"], "wrong-session")
        orch = store.get_orch(self.con, "owner")
        self.assertEqual((orch["id"], orch["session_id"], orch["job_id"], orch["stopped_at"]),
                         ("owner", "wrong-session", "new-job", None))
        self.assertEqual(self.accept_copy(path), result)
        self.assertEqual(self.reload()["phase"], "complete")
        self.assertEqual(len(self.rt.starts), 1)
        self.assertEqual(self.rt.stops, ["old-job"])

    def test_copy_acceptance_requires_exact_receipt_job_session_and_digest(self):
        path, roster = self.copy_evidence()
        for args in ({"job": "another-job"}, {"session": "another-session"}, {"digest": "0" * 64}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.accept_copy(path, **args)
        self.assertIsNotNone(store.get_orch(self.con, "owner")["stopped_at"])
        self.assertEqual(len(self.rt.starts), 1)

    def test_unproven_copy_lineage_or_original_config_is_rejected(self):
        path, roster = self.copy_evidence()
        original = json.loads(json.dumps(roster))
        cases = (lambda r: r["workers"]["new-job"]["dispatch"]["launch"].update(fork=False),
                 lambda r: r["workers"]["new-job"]["dispatch"]["launch"].update(sessionId="unrelated"),
                 lambda r: r["workers"]["new-job"]["dispatch"]["launch"].update(restoresTranscript=False),
                 lambda r: r["workers"]["new-job"]["dispatch"].update(cwd="/srv/another"),
                 lambda r: r["workers"]["new-job"]["dispatch"]["launch"]["flagArgs"].__setitem__(1, "another-model"))
        for change in cases:
            data = json.loads(json.dumps(original))
            change(data)
            self.save_roster(path, data)
            with self.assertRaises(ValueError):
                self.accept_copy(path)
        self.assertEqual(self.rt.stops, ["old-job"])
        self.assertEqual(len(self.rt.starts), 1)

    def test_copy_acceptance_requires_current_deployment_mcp_entry(self):
        path, roster = self.copy_evidence()
        mcp = self.home / "mcp.json"
        data = json.loads(mcp.read_text())
        data["mcpServers"]["orchd"]["command"] = "/srv/old-venv/bin/python"
        mcp.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "this deployment"):
            self.accept_copy(path)

    def test_copy_acceptance_rejects_mcp_profile_pointing_at_another_state(self):
        path, roster = self.copy_evidence()
        mcp = self.home / "mcp.json"
        data = json.loads(mcp.read_text())
        data["mcpServers"]["orchd"]["env"]["ORCHD_HOME"] = "/srv/another-state"
        mcp.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "profile"):
            self.accept_copy(path)
        self.assertIsNotNone(store.get_orch(self.con, "owner")["stopped_at"])

    def test_copy_acceptance_requires_fresh_live_socket_and_unambiguous_session(self):
        path, roster = self.copy_evidence()
        with mock.patch.object(self.rt, "socket_listening", return_value=False), self.assertRaises(ValueError):
            self.accept_copy(path)
        for session in ("original-session", "wrong-session", ""):
            self.rt.jobs["competitor"] = dict(id="competitor", sessionId=session, pid=999, status="idle")
            with self.subTest(session=session), self.assertRaisesRegex(ValueError, "ambiguous"):
                self.accept_copy(path)
        self.assertEqual(self.rt.stops, ["old-job"])
        self.assertEqual(len(self.rt.starts), 1)

    def test_controller_change_and_unproven_roster_host_prevent_copy_acceptance(self):
        path, roster = self.copy_evidence()
        roster["workers"]["new-job"]["pid"] = 999
        self.save_roster(path, roster)
        with mock.patch.object(orch_reload, "_process_parent", return_value=444), self.assertRaises(ValueError):
            self.accept_copy(path)
        self.con.execute("UPDATE orchs SET stopped_at=123 WHERE id='owner'")
        with self.assertRaisesRegex(ValueError, "changed"):
            self.accept_copy(path)

    def recovery_evidence(self):
        path, roster = self.copy_evidence()
        # Model a genuine launch whose return was lost: no inferred receipt.
        self.con.execute("UPDATE orch_reload_operations SET phase='spawn_pending',receipt=NULL,"
                         "error='spawn_acceptance_unknown' WHERE operation_id=?", (self.op,))
        row = orch_reload.status(self.con, self.op)
        receipt = {"version": 1, "operation_id": self.op,
                   "original": {"orch_id": "owner", "session_id": "original-session", "job_id": "old-job",
                       "model": "example-model", "home": str(self.home), "socket": str(self.home / "o.sock")},
                   "launcher": {"command": str(Path(sys.executable).resolve()),
                                "config": row["preflight"]["provider"]["config"]},
                   "started_at": row["created_at"], "finished_at": time.time(), "exit_code": 0,
                   "native_stdout": "claude attach new-job",
                   "receipt": {"job_id": "new-job", "session_id": "wrong-session",
                               "socket": str(self.home / "o.sock")}}
        source = self.home / "launch-receipt.json"
        self.save_roster(source, receipt)
        self.save_roster(path, roster)
        return source, receipt, path, roster

    def attach_recovery(self, source, roster):
        return orch_reload.attach_recovery_receipt(self.con, self.rt, self.op, source,
            hashlib.sha256(source.read_bytes()).hexdigest(), roster,
            hashlib.sha256(roster.read_bytes()).hexdigest())

    def test_recovery_receipt_attach_is_evidence_only_and_reuses_copy_acceptance(self):
        source, receipt, path, roster = self.recovery_evidence()
        before = dict(store.get_orch(self.con, "owner"))
        original = orch_reload.status(self.con, self.op)
        result = self.attach_recovery(source, path)
        self.assertEqual(result["phase"], "held")
        self.assertEqual(result["error"], "returned_session_mismatch")
        self.assertEqual(result["preflight"], original["preflight"])
        self.assertEqual(result["recovery"]["previous_error"], "spawn_acceptance_unknown")
        self.assertEqual(result["recovery"]["launcher"], receipt["launcher"])
        self.assertEqual(dict(store.get_orch(self.con, "owner")), before)
        self.assertEqual(self.attach_recovery(source, path), result)
        self.assertEqual(self.accept_copy(path)["phase"], "complete")
        self.assertEqual(len(self.rt.starts), 1)
        self.assertEqual(self.rt.stops, ["old-job"])

    def test_recovery_rejects_forged_identity_digest_permissions_and_native_receipt_without_mutation(self):
        source, receipt, path, roster = self.recovery_evidence()
        before = orch_reload.status(self.con, self.op)
        original = json.loads(json.dumps(receipt))
        changes = (lambda r: r.update(operation_id=str(uuid.uuid4())),
                   lambda r: r["original"].update(session_id="other"),
                   lambda r: r["original"].update(job_id="other"),
                   lambda r: r["receipt"].update(job_id="other"),
                   lambda r: r["launcher"].update(config="/srv/wrong-profile"),
                   lambda r: r.update(native_stdout="claude attach unrelated"),
                   lambda r: r.update(exit_code=1),
                   lambda r: r.update(started_at=False),
                   lambda r: r.update(finished_at=float("nan")))
        for change in changes:
            data = json.loads(json.dumps(original)); change(data)
            self.save_roster(source, data)
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.attach_recovery(source, path)
            self.assertEqual(orch_reload.status(self.con, self.op), before)
        self.save_roster(source, original);source.chmod(0o644)
        with self.assertRaises(ValueError): self.attach_recovery(source, path)
        source.chmod(0o600)
        with self.assertRaises(ValueError):
            orch_reload.attach_recovery_receipt(self.con, self.rt, self.op, source, "0" * 64,
                                               path, hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(orch_reload.status(self.con, self.op), before)

    def test_recovery_requires_all_existing_copy_proofs_before_attachment(self):
        source, receipt, path, roster = self.recovery_evidence()
        before = orch_reload.status(self.con, self.op)
        with mock.patch.object(self.rt, "socket_listening", return_value=False), self.assertRaises(ValueError):
            self.attach_recovery(source, path)
        roster["workers"]["new-job"]["dispatch"]["launch"]["fork"] = False
        self.save_roster(path, roster)
        with self.assertRaises(ValueError):self.attach_recovery(source, path)
        self.assertEqual(orch_reload.status(self.con, self.op), before)

    def test_existing_copy_acceptance_rejects_another_parent_copy_uuid(self):
        path, roster = self.copy_evidence()
        self.rt.jobs["other-copy"] = dict(self.rt.jobs["new-job"], id="other-copy", sessionId="third-uuid")
        roster["workers"]["other-copy"] = json.loads(json.dumps(roster["workers"]["new-job"]))
        roster["workers"]["other-copy"]["sessionId"] = "third-uuid"
        roster["workers"]["other-copy"]["dispatch"]["sessionId"] = "third-uuid"
        roster["workers"]["other-copy"]["dispatch"]["short"] = "other-copy"
        self.save_roster(path, roster)
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            self.accept_copy(path)
        self.assertIsNotNone(store.get_orch(self.con, "owner")["stopped_at"])

    def test_roster_only_accepted_competitor_refuses_attachment_and_existing_acceptance(self):
        source, receipt, path, roster = self.recovery_evidence()
        before = orch_reload.status(self.con, self.op)
        original = json.loads(json.dumps(roster))
        def competing_roster(kind):
            data = json.loads(json.dumps(original))
            candidate = json.loads(json.dumps(data["workers"]["new-job"]))
            candidate["sessionId"] = candidate["dispatch"]["sessionId"] = "third-uuid"
            candidate["dispatch"]["short"] = "pending-copy"
            if kind == "same-parent-foreign-home":
                candidate["cwd"] = candidate["dispatch"]["cwd"] = "/srv/another-home"
            else:
                candidate["dispatch"]["launch"].update(mode="fresh", sessionId="unrelated")
            data["workers"]["pending-copy"] = candidate
            return data
        # Native acceptance precedes publication in the public jobs listing.
        self.assertNotIn("pending-copy", self.rt.jobs)
        for kind in ("same-parent-foreign-home", "same-home-other-parent"):
            self.save_roster(path, competing_roster(kind))
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, "ambiguous"):
                self.attach_recovery(source, path)
            self.assertEqual(orch_reload.status(self.con, self.op), before)
        self.con.execute("UPDATE orch_reload_operations SET phase='held',error='returned_session_mismatch',"
                         "receipt=? WHERE operation_id=?", (json.dumps(receipt["receipt"]), self.op))
        for kind in ("same-parent-foreign-home", "same-home-other-parent"):
            self.save_roster(path, competing_roster(kind))
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, "ambiguous"):
                self.accept_copy(path)
        self.assertIsNotNone(store.get_orch(self.con, "owner")["stopped_at"])

    def test_roster_worker_unknown_or_conflicting_scope_refuses_without_public_job(self):
        source, receipt, path, roster = self.recovery_evidence()
        original = json.loads(json.dumps(roster))
        cases = ({}, {"cwd": "relative-provider-cwd"}, {"cwd": 42},
                 {"cwd": "  "}, {"cwd": "/srv/foreign", "dispatch": {"cwd": "/srv/different"}},
                 {"cwd": "/srv/foreign", "dispatch": {"cwd": "relative"}},
                 {"cwd": "/srv/foreign", "dispatch": {"cwd": 42}},
                 {"cwd": "/srv/foreign", "dispatch": {"cwd": None}},
                 {"cwd": "/srv/foreign", "dispatch": []},
                 {"cwd": "/srv/foreign", "dispatch": {"launch": []}})
        for candidate in cases:
            with self.subTest(candidate=candidate):
                self.con.execute("UPDATE orch_reload_operations SET phase='spawn_pending',receipt=NULL,"
                                 "recovery=NULL,error='spawn_acceptance_unknown' WHERE operation_id=?", (self.op,))
                before = orch_reload.status(self.con, self.op)
                data = json.loads(json.dumps(original));data["workers"]["pending-unknown"] = dict(candidate, sessionId="foreign-session")
                self.save_roster(path, data)
                self.assertNotIn("pending-unknown", self.rt.jobs)
                with self.assertRaisesRegex(ValueError, "malformed|ambiguous"):
                    self.attach_recovery(source, path)
                self.assertEqual(orch_reload.status(self.con, self.op), before)
        self.con.execute("UPDATE orch_reload_operations SET phase='held',error='returned_session_mismatch',"
                         "receipt=? WHERE operation_id=?", (json.dumps(receipt["receipt"]), self.op))
        self.save_roster(path, dict(original, workers=dict(original["workers"], **{"pending-unknown": {}})))
        with self.assertRaisesRegex(ValueError, "malformed|ambiguous"):
            self.accept_copy(path)
        self.assertIsNotNone(store.get_orch(self.con, "owner")["stopped_at"])

    def test_raw_original_or_unknown_session_refuses_even_with_foreign_absolute_cwd(self):
        source, receipt, path, roster = self.recovery_evidence()
        original = json.loads(json.dumps(roster))
        cases = ({"sessionId": "original-session"}, {"sessionId": "wrong-session"}, {},
                 {"sessionId": None}, {"sessionId": ""}, {"sessionId": "  "}, {"sessionId": 42},
                 {"sessionId": "foreign-session", "dispatch": {"sessionId": "original-session"}},
                 {"sessionId": "foreign-session", "dispatch": {"sessionId": None}},
                 {"sessionId": "foreign-session", "dispatch": {"sessionId": ""}},
                 {"sessionId": "foreign-session", "dispatch": {"sessionId": 42}})
        for candidate in cases:
            with self.subTest(candidate=candidate):
                self.con.execute("UPDATE orch_reload_operations SET phase='spawn_pending',receipt=NULL,"
                                 "recovery=NULL,error='spawn_acceptance_unknown' WHERE operation_id=?", (self.op,))
                before = orch_reload.status(self.con, self.op)
                data = json.loads(json.dumps(original))
                data["workers"]["pending-identity"] = dict(candidate, cwd="/srv/foreign-home")
                self.save_roster(path, data)
                self.assertNotIn("pending-identity", self.rt.jobs)
                with self.assertRaisesRegex(ValueError, "malformed|ambiguous"):
                    self.attach_recovery(source, path)
                self.assertEqual(orch_reload.status(self.con, self.op), before)
        self.con.execute("UPDATE orch_reload_operations SET phase='held',error='returned_session_mismatch',"
                         "receipt=? WHERE operation_id=?", (json.dumps(receipt["receipt"]), self.op))
        data = json.loads(json.dumps(original))
        data["workers"]["pending-identity"] = {"sessionId": "original-session", "cwd": "/srv/foreign-home"}
        self.save_roster(path, data)
        with self.assertRaisesRegex(ValueError, "malformed|ambiguous"):
            self.accept_copy(path)
        self.assertIsNotNone(store.get_orch(self.con, "owner")["stopped_at"])

    def test_raw_roster_identity_token_contract_refuses_invalid_metadata_without_mutation(self):
        source, receipt, path, roster = self.recovery_evidence()
        original = json.loads(json.dumps(roster))
        valid = {"sessionId": "foreign-session", "cwd": "/srv/foreign-home"}
        cases = [(key, valid) for key in ("", " ", " leading", "trailing ", "inner whitespace")]
        cases += [("foreign-worker", dict(valid, sessionId=value))
                  for value in (" leading", "trailing ", "inner whitespace")]
        cases += [("foreign-worker", dict(valid, dispatch={"short": value}))
                  for value in ("old-job", "", None, 42, " ", " foreign-worker")]
        cases += [("foreign-worker", dict(valid, dispatch={"sessionId": value}))
                  for value in (" foreign-session", "foreign-session ", "foreign session")]
        cases += [("foreign-worker", dict(valid, dispatch={"launch": {"mode": "resume", "sessionId": value}}))
                  for value in (None, "", 42, " ", " original-session", "foreign parent")]
        for key, candidate in cases:
            with self.subTest(key=key, candidate=candidate):
                self.con.execute("UPDATE orch_reload_operations SET phase='spawn_pending',receipt=NULL,"
                                 "recovery=NULL,error='spawn_acceptance_unknown' WHERE operation_id=?", (self.op,))
                before = orch_reload.status(self.con, self.op)
                data = json.loads(json.dumps(original));data["workers"][key] = candidate
                self.save_roster(path, data)
                with self.assertRaisesRegex(ValueError, "malformed|ambiguous"):
                    self.attach_recovery(source, path)
                self.assertEqual(orch_reload.status(self.con, self.op), before)
        self.con.execute("UPDATE orch_reload_operations SET phase='held',error='returned_session_mismatch',"
                         "receipt=? WHERE operation_id=?", (json.dumps(receipt["receipt"]), self.op))
        data = json.loads(json.dumps(original))
        data["workers"]["foreign-worker"] = dict(valid, dispatch={"short": "old-job"})
        self.save_roster(path, data)
        with self.assertRaisesRegex(ValueError, "malformed|ambiguous"):
            self.accept_copy(path)
        self.assertIsNotNone(store.get_orch(self.con, "owner")["stopped_at"])

    def test_second_copy_with_different_uuid_but_same_parent_rejects_attachment_and_acceptance(self):
        source, receipt, path, roster = self.recovery_evidence()
        self.rt.jobs["other-copy"] = dict(self.rt.jobs["new-job"], id="other-copy", sessionId="third-uuid")
        roster["workers"]["other-copy"] = json.loads(json.dumps(roster["workers"]["new-job"]))
        roster["workers"]["other-copy"]["sessionId"] = "third-uuid"
        roster["workers"]["other-copy"]["dispatch"]["sessionId"] = "third-uuid"
        roster["workers"]["other-copy"]["dispatch"]["short"] = "other-copy"
        self.save_roster(path, roster)
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            self.attach_recovery(source, path)
        self.con.execute("UPDATE orch_reload_operations SET phase='held',error='returned_session_mismatch',"
                         "receipt=? WHERE operation_id=?", (json.dumps(receipt["receipt"]), self.op))
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            self.accept_copy(path)

    def test_recovery_replay_revalidates_live_evidence_and_refuses_conflicting_attachment(self):
        source, receipt, path, roster = self.recovery_evidence()
        first = self.attach_recovery(source, path)
        with mock.patch.object(self.rt, "socket_listening", return_value=False), self.assertRaises(ValueError):
            self.attach_recovery(source, path)
        receipt["finished_at"] += 0.00001
        self.save_roster(source, receipt)
        with self.assertRaises(ValueError):self.attach_recovery(source, path)
        self.assertEqual(orch_reload.status(self.con, self.op), first)

    def test_recovery_rejects_stale_roster_missing_stop_or_reader_change(self):
        source, receipt, path, roster = self.recovery_evidence()
        before = orch_reload.status(self.con, self.op)
        os.utime(path, (1, 1))
        with self.assertRaises(ValueError): self.attach_recovery(source, path)
        self.save_roster(path, roster)
        self.rt.claude = "/srv/bin/another-reader"
        with self.assertRaises(ValueError): self.attach_recovery(source, path)
        self.rt.claude = "/srv/bin/example-claude"
        self.assertEqual(orch_reload.status(self.con, self.op), before)
        self.con.execute("UPDATE orch_reload_operations SET stop_evidence=NULL WHERE operation_id=?", (self.op,))
        with self.assertRaises(ValueError): self.attach_recovery(source, path)
        self.assertIsNone(orch_reload.status(self.con, self.op)["recovery"])

    def test_recovery_failed_mcp_or_pid_proof_and_changed_controller_do_not_attach(self):
        source, receipt, path, roster = self.recovery_evidence()
        before = orch_reload.status(self.con, self.op)
        roster["workers"]["new-job"]["pid"] = 999
        self.save_roster(path, roster)
        with mock.patch.object(orch_reload, "_process_parent", return_value=444), self.assertRaises(ValueError):
            self.attach_recovery(source, path)
        roster["workers"]["new-job"]["pid"] = 101
        self.save_roster(path, roster)
        mcp = self.home / "mcp.json"
        data = json.loads(mcp.read_text());data["mcpServers"]["orchd"]["command"] = "/srv/wrong-python"
        mcp.write_text(json.dumps(data))
        with self.assertRaises(ValueError): self.attach_recovery(source, path)
        self.assertEqual(orch_reload.status(self.con, self.op), before)
        self.con.execute("UPDATE orchs SET stopped_at=123 WHERE id='owner'")
        with self.assertRaises(ValueError): self.attach_recovery(source, path)
        self.assertIsNone(orch_reload.status(self.con, self.op)["recovery"])

    def test_missing_or_relative_cwd_on_active_other_uuid_refuses_attachment_and_acceptance(self):
        source, receipt, path, roster = self.recovery_evidence()
        before = orch_reload.status(self.con, self.op)
        self.rt.jobs["unknown-copy"] = dict(id="unknown-copy", sessionId="third-uuid", pid=999, status="idle")
        for cwd in (None, "", "  ", 42, "relative-provider-cwd", "."):
            self.rt.jobs["unknown-copy"]["cwd"] = cwd
            with self.subTest(cwd=cwd), self.assertRaisesRegex(ValueError, "ambiguous"):
                self.attach_recovery(source, path)
            self.assertEqual(orch_reload.status(self.con, self.op), before)
        self.con.execute("UPDATE orch_reload_operations SET phase='held',error='returned_session_mismatch',"
                         "receipt=? WHERE operation_id=?", (json.dumps(receipt["receipt"]), self.op))
        for cwd in (None, "", "  ", 42, "relative-provider-cwd", "."):
            self.rt.jobs["unknown-copy"]["cwd"] = cwd
            with self.subTest(cwd=cwd), self.assertRaisesRegex(ValueError, "ambiguous"):
                self.accept_copy(path)
        self.assertIsNotNone(store.get_orch(self.con, "owner")["stopped_at"])

    def test_recovery_keeps_unrelated_author_job_and_requires_operator(self):
        source, receipt, path, roster = self.recovery_evidence()
        self.rt.jobs["author"] = dict(id="author", sessionId="author-original", cwd="/srv/author", pid=999, status="idle")
        roster["workers"]["author"] = {"sessionId": "author-original", "cwd": "/srv/author", "dispatch": {
            "sessionId": "author-original", "cwd": "/srv/author", "launch": {"mode": "resume", "sessionId": "unrelated-parent"}}}
        roster["workers"]["provider:foreign_worker"] = {
            "sessionId": "provider:foreign_session", "cwd": "/srv/foreign-fresh", "dispatch": {
                "short": "provider:foreign_worker", "sessionId": "provider:foreign_session",
                "cwd": "/srv/foreign-fresh", "launch": {"mode": "provider-fresh-mode"}}}
        self.save_roster(path, roster)
        with mock.patch.dict(os.environ, {"ORCHD_TASK_ID": "author"}), self.assertRaises(PermissionError):
            self.attach_recovery(source, path)
        self.assertIsNone(orch_reload.status(self.con, self.op)["recovery"])
        self.assertEqual(self.attach_recovery(source, path)["phase"], "held")
        self.assertEqual(self.rt.jobs["author"]["pid"], 999)

    def test_cli_attachment_keeps_noncomplete_exit_and_has_no_launch(self):
        source, receipt, path, roster = self.recovery_evidence()
        args = ["attach-recovery-receipt", "--state", str(self.home / "orchd.db"), "--operation", self.op,
                "--receipt-file", str(source), "--receipt-sha256", hashlib.sha256(source.read_bytes()).hexdigest(),
                "--roster-file", str(path), "--roster-sha256", hashlib.sha256(path.read_bytes()).hexdigest()]
        with mock.patch("orchd.runtime.Runtime", return_value=self.rt), mock.patch("builtins.print"):
            self.assertEqual(orch_reload.main(args), 2)
        self.assertEqual(orch_reload.status(self.con, self.op)["phase"], "held")
        self.assertEqual(len(self.rt.starts), 1)

    def test_real_native_no_job_failure_preserves_stage_returncode_and_only_output_digests(self):
        runtime = Runtime()
        runtime.claude = self.rt.claude
        native_stdout = "PRIVATE PROMPT stdout, no receipt"
        native_stderr = "PRIVATE TOKEN stderr, provider rejected"
        results = [subprocess.CompletedProcess([], 0, "example-version", ""),
                   subprocess.CompletedProcess([], 7, native_stdout, native_stderr)]
        with mock.patch.object(runtime, "run", side_effect=results) as run, \
                mock.patch.object(runtime, "live_jobs", side_effect=self.rt.live_jobs), \
                mock.patch.object(runtime, "stop_worker", side_effect=self.rt.stop_worker), \
                mock.patch.object(runtime, "socket_listening", side_effect=self.rt.socket_listening), \
                mock.patch.object(runtime, "orch_socket_path", return_value=str(self.home / "o.sock")):
            row = orch_reload.reload(self.con, runtime, "owner", self.op, "original-session",
                                     "old-job", "example-model")
        failure = row["spawn_failure"]
        self.assertEqual(failure["launch_stage"], "native_no_job_receipt")
        self.assertEqual(failure["returncode"], 7)
        for key, value in (("stdout", native_stdout), ("stderr", native_stderr)):
            self.assertEqual(failure[key + "_sha256"], hashlib.sha256(value.encode()).hexdigest())
            self.assertEqual(failure[key + "_bytes"], len(value.encode()))
        self.assertNotIn("PRIVATE", json.dumps(row))
        self.assertIsNone(row["receipt"])
        self.assertEqual(row["phase"], "spawn_pending")
        self.assertIsNotNone(store.get_orch(self.con, "owner")["stopped_at"])
        self.assertEqual(run.call_count, 2)

    def test_spawn_failure_keeps_structured_diagnostics_without_prompt_or_secret_text(self):
        error = RuntimeError("SECRET prompt body")
        error.launch_stage = "SECRET not-an-allowlisted-stage"
        with mock.patch.object(self.rt, "start_orch", side_effect=error):
            row = self.reload()
        self.assertEqual(row["error"], "spawn_acceptance_unknown")
        self.assertEqual(row["spawn_failure"]["type"], "RuntimeError")
        self.assertNotIn("SECRET", json.dumps(row))
        self.assertIsNone(row["receipt"])

    def test_malformed_old_row_is_not_positive_retirement(self):
        row = dict(old_job="old-job", session_id="original-session", home=str(self.home), preflight=None)
        for old in ({}, {"pid": None, "status": None},
                    {"id": "old-job", "sessionId": "another", "pid": None, "status": None}):
            self.assertIsNone(orch_reload._stop_proof(row, self.rt, {"old-job": old}, False))

    def test_native_start_writes_mcp_with_current_deployment_python(self):
        sock = str(self.home / "o.sock")
        with mock.patch.object(Runtime, "orch_socket_path", return_value=sock), \
                mock.patch.object(Runtime, "start_claude", return_value=("new", "original-session")):
            Runtime().start_orch("owner", "example-model", self.home, resume="original-session")
        config = json.loads((self.home / "mcp.json").read_text())
        self.assertEqual(config["mcpServers"]["orchd"]["command"], sys.executable)


if __name__ == "__main__":
    unittest.main()
