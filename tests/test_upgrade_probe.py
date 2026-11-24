"""Exercise the real MCP acknowledgement without starting a provider or a worker."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from orchd import entry_ops, mcp_server, paths, store, upgrade_probe


class UpgradeProbeTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.env = patch.dict(os.environ, {"ORCHD_HOME": tmp.name, "ORCHD_EXECUTABLE": "synthetic-orchd"})
        self.env.start()
        self.addCleanup(self.env.stop)
        for name in ("ORCHD_ORCH_ID", "ORCHD_WORKTREE", "ORCHD_TASK_ID"):
            os.environ.pop(name, None)
        self.con = store.connect(Path(tmp.name) / "orchd.db")
        self.addCleanup(self.con.close)
        store.register_orch(self.con, "onew0001", "claude", session_id="new-session", job_id="new-job",
                            socket="synthetic.sock", model="synthetic-model")
        self.op = entry_ops.start("upgrade", entry_id="desktop", spawn=lambda *a: SimpleNamespace(pid=1))["op_id"]
        record = entry_ops.read(self.op)
        record.update(state="running", new_orch="onew0001", restart_state="done")
        entry_ops._write(record)
        self.sent = []

    def runtime(self, send=None, *, jobs=True, listening=True):
        job = dict(id="new-job", pid=12345, status="idle", sessionId="new-session", cwd=str(paths.orch_home()))
        return SimpleNamespace(send_uds=send or (lambda *a: self.sent.append(a)),
                               live_jobs=lambda: {"new-job": job} if jobs else {},
                               socket_listening=lambda *a: listening)

    def create_pending(self):
        rt = self.runtime()
        return upgrade_probe.verify(self.con, rt, self.op, timeout=0)

    def ack(self, *, actor="onew0001", fields=None, challenge=None):
        request = json.loads(upgrade_probe._path(self.op, ".probe.json").read_text())
        args = dict(op_id=self.op, challenge=challenge or request["challenge"],
                    dispatch_fields=upgrade_probe.dispatch_fields() if fields is None else fields)
        with patch.dict(os.environ, {"ORCHD_ORCH_ID": actor}):
            return mcp_server.handle({"id": 1, "method": "tools/call", "params": {
                "name": "upgrade_probe", "arguments": args}}, self.con, None)["result"]

    def test_new_controller_exercises_the_mcp_and_the_exact_dispatch_schema(self):
        def send(*args):
            self.sent.append(args)
            self.assertFalse(self.ack().get("isError"))
        result = upgrade_probe.verify(self.con, self.runtime(send), self.op, timeout=0)
        self.assertEqual(result["state"], "verified")
        self.assertEqual(result["session_id"], "new-session")
        contract = upgrade_probe.dispatch_contract()
        self.assertEqual(result["dispatch_tool"], contract["dispatch_tool"])
        self.assertEqual(result["dispatch_batch_tool"], contract["dispatch_batch_tool"])
        self.assertEqual(self.con.execute("SELECT count(*) FROM tasks").fetchone()[0], 0)
        self.assertEqual(len(self.sent), 1)

    def test_prompt_hydrates_exact_alias_without_supplying_the_field_answers(self):
        contract = dict(upgrade_probe.dispatch_contract(), dispatch_fields=["synthetic_unseen_field_a", "synthetic_unseen_field_b"])
        with patch.object(upgrade_probe, "dispatch_contract", return_value=contract):
            self.create_pending()
        message = self.sent[0][2]
        self.assertIn("select:mcp__orchd__" + contract["dispatch_tool"], message)
        self.assertIn(contract["dispatch_batch_tool"], message)
        self.assertNotIn("synthetic_unseen_field_a", message)
        self.assertNotIn("synthetic_unseen_field_b", message)
        request = json.loads(upgrade_probe._path(self.op, ".probe.json").read_text(encoding="utf-8"))
        for name, value in contract.items():
            self.assertEqual(request[name], value)

    def test_descriptor_change_with_the_same_fields_invalidates_request_and_ack(self):
        self.create_pending()
        before = upgrade_probe._path(self.op, ".probe.json").read_bytes()
        for name in ("dispatch_tool", "dispatch_batch_tool"):
            with self.subTest(name=name):
                contract = dict(upgrade_probe.dispatch_contract())
                contract[name] += "_changed"
                with patch.object(upgrade_probe, "dispatch_contract", return_value=contract):
                    self.assertTrue(self.ack()["isError"])
                    with self.assertRaisesRegex(ValueError, "Canonical dispatch contract changed"):
                        self.create_pending()
                self.assertEqual(upgrade_probe._path(self.op, ".probe.json").read_bytes(), before)
                self.assertFalse(upgrade_probe._path(self.op, ".probe-ack.json").exists())

    def test_legacy_request_without_alias_binding_requires_a_new_operation(self):
        self.create_pending()
        path = upgrade_probe._path(self.op, ".probe.json")
        request = json.loads(path.read_text(encoding="utf-8"))
        request.pop("dispatch_tool")
        request.pop("dispatch_batch_tool")
        path.write_text(json.dumps(request), encoding="utf-8")
        before = path.read_bytes()
        self.assertTrue(self.ack()["isError"])
        with self.assertRaisesRegex(ValueError, "Canonical dispatch contract changed"):
            self.create_pending()
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(len(self.sent), 1)

    def test_successful_replay_cannot_reuse_a_receipt_after_the_contract_changes(self):
        self.create_pending()
        self.assertFalse(self.ack().get("isError"))
        record = entry_ops.read(self.op)
        record["state"] = "needs_session_refresh"
        entry_ops._write(record)
        self.assertEqual(self.create_pending()["state"], "verified")
        for name in ("dispatch_tool", "dispatch_batch_tool"):
            contract = dict(upgrade_probe.dispatch_contract())
            contract[name] += "_changed"
            with patch.object(upgrade_probe, "dispatch_contract", return_value=contract):
                with self.assertRaisesRegex(ValueError, "Canonical dispatch contract changed"):
                    self.create_pending()
        self.assertEqual(len(self.sent), 1)

    def test_unbound_or_wrong_alias_ack_file_is_not_consumed_or_replayed(self):
        self.create_pending()
        self.assertFalse(self.ack().get("isError"))
        path = upgrade_probe._path(self.op, ".probe-ack.json")
        valid = json.loads(path.read_text(encoding="utf-8"))
        for name in ("dispatch_tool", "dispatch_batch_tool"):
            for value in (None, "wrong_alias"):
                with self.subTest(name=name, value=value):
                    receipt = dict(valid)
                    if value is None:
                        receipt.pop(name)
                    else:
                        receipt[name] = value
                    path.write_text(json.dumps(receipt), encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, "Stored tool receipt differs"):
                        self.create_pending()
                    self.assertTrue(self.ack()["isError"])

    def test_contract_is_rechecked_when_a_receipt_arrives_during_wait(self):
        contract = dict(upgrade_probe.dispatch_contract())
        def delayed_receipt(_):
            self.assertFalse(self.ack().get("isError"))
            contract["dispatch_tool"] += "_changed"
        with patch.object(upgrade_probe, "dispatch_contract", return_value=contract):
            with self.assertRaisesRegex(ValueError, "Canonical dispatch contract changed"):
                upgrade_probe.verify(self.con, self.runtime(), self.op, timeout=1, sleep=delayed_receipt)
        self.assertEqual(len(self.sent), 1)

    def test_stale_dispatch_fields_cannot_complete_the_probe(self):
        self.assertEqual(self.create_pending()["state"], "pending")
        fields = upgrade_probe.dispatch_fields()
        self.assertTrue(self.ack(fields=fields[:-1])["isError"])
        self.assertFalse(upgrade_probe._path(self.op, ".probe-ack.json").exists())

    def test_timeout_retry_does_not_send_another_prompt_or_restart(self):
        self.create_pending()
        self.create_pending()
        self.assertEqual(len(self.sent), 1)
        record = entry_ops.read(self.op)
        record["state"] = "needs_session_refresh"
        entry_ops._write(record)
        self.assertFalse(self.ack().get("isError"))
        self.assertEqual(self.create_pending()["state"], "verified")
        self.assertEqual(entry_ops.read(self.op)["state"], "done")
        self.assertEqual(len(self.sent), 1)

    def test_other_orch_wrong_nonce_and_worker_cannot_acknowledge(self):
        self.create_pending()
        self.assertTrue(self.ack(actor="oold0001")["isError"])
        self.assertTrue(self.ack(challenge="wrong")["isError"])
        with patch.dict(os.environ, {"ORCHD_WORKTREE": "synthetic-worktree"}):
            self.assertTrue(self.ack()["isError"])
        self.assertFalse(upgrade_probe._path(self.op, ".probe-ack.json").exists())

    def test_receipt_cannot_verify_a_replaced_controller(self):
        self.create_pending()
        self.assertFalse(self.ack().get("isError"))
        self.con.execute("UPDATE orchs SET session_id='other-session' WHERE id='onew0001'")
        with self.assertRaisesRegex(ValueError, "controller or schema changed"):
            self.create_pending()

    def test_uncertain_delivery_is_not_retried(self):
        def uncertain(*args):
            self.sent.append(args)
            raise OSError("synthetic uncertain delivery")
        result = upgrade_probe.verify(self.con, self.runtime(uncertain), self.op, timeout=0)
        self.assertEqual(result["reason"], "probe_delivery_unknown")
        self.create_pending()
        self.assertEqual(len(self.sent), 1)

    def test_dead_controller_cannot_consume_a_persisted_ack(self):
        self.create_pending()
        self.assertFalse(self.ack().get("isError"))
        record = entry_ops.read(self.op)
        record["state"] = "needs_session_refresh"
        entry_ops._write(record)
        result = upgrade_probe.verify(self.con, self.runtime(jobs=False, listening=False), self.op, timeout=0)
        self.assertEqual(result["state"], "pending")
        self.assertEqual(entry_ops.read(self.op)["state"], "needs_session_refresh")

    def test_successful_verification_retry_returns_the_same_receipt(self):
        self.create_pending()
        self.assertFalse(self.ack().get("isError"))
        record = entry_ops.read(self.op)
        record["state"] = "needs_session_refresh"
        entry_ops._write(record)
        first = self.create_pending()
        second = self.create_pending()
        self.assertEqual(first, second)
        self.assertEqual(len(self.sent), 1)

    def test_fresh_cli_finishes_the_existing_pending_probe(self):
        self.create_pending()
        self.assertFalse(self.ack().get("isError"))
        record = entry_ops.read(self.op)
        record["state"] = "needs_session_refresh"
        entry_ops._write(record)
        code = ("import sys; from types import SimpleNamespace; from orchd import cli, paths; "
                "job=dict(id='new-job',pid=12345,status='idle',sessionId='new-session',cwd=str(paths.orch_home())); "
                "cli.Runtime=lambda: SimpleNamespace(live_jobs=lambda:{'new-job':job},socket_listening=lambda *a:True); "
                "sys.exit(cli.main(['_entry-verify',sys.argv[1]]))")
        result = subprocess.run([sys.executable, "-B", "-c", code, self.op],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["state"], "verified")
        self.assertEqual(entry_ops.public(entry_ops.read(self.op))["state"], "done")
        self.assertEqual(len(self.sent), 1)

    def test_same_owner_controller_verification_does_not_change_task_or_controller_identity(self):
        op = "a123456789ab"
        before = dict(store.get_orch(self.con, "onew0001"))
        def send(*args):
            request = json.loads(upgrade_probe._path(op, ".probe.json").read_text(encoding="utf-8"))
            with patch.dict(os.environ, {"ORCHD_ORCH_ID": "onew0001"}):
                upgrade_probe.acknowledge(self.con, "onew0001", op, request["challenge"],
                                          upgrade_probe.dispatch_fields())
            self.sent.append(args)
        result = upgrade_probe.verify_current(self.con, self.runtime(send), op, "onew0001",
                                              "new-session", "new-job", timeout=0)
        self.assertEqual(result["state"], "verified")
        self.assertEqual(dict(store.get_orch(self.con, "onew0001")), before)
        self.assertEqual(self.con.execute("SELECT count(*) FROM tasks").fetchone()[0], 0)
        replay = upgrade_probe.verify_current(self.con, self.runtime(send), op, "onew0001",
                                              "new-session", "new-job", timeout=0)
        self.assertEqual(replay, result)
        self.assertEqual(len(self.sent), 1)
        with self.assertRaisesRegex(ValueError, "identity does not match"):
            upgrade_probe.verify_current(self.con, self.runtime(), op, "onew0001", "wrong", "new-job", timeout=0)

    def test_wrong_provider_session_cwd_or_closed_socket_cannot_verify(self):
        self.create_pending()
        self.assertFalse(self.ack().get("isError"))
        for field, value in (("sessionId", "wrong"), ("cwd", "other-home"), ("pid", None)):
            rt = self.runtime()
            job = rt.live_jobs()["new-job"]
            job[field] = value
            rt.live_jobs = lambda: {"new-job": job}
            self.assertEqual(upgrade_probe.verify(self.con, rt, self.op, timeout=0)["state"], "pending")
        self.assertEqual(upgrade_probe.verify(self.con, self.runtime(listening=False), self.op, timeout=0)["state"],
                         "pending")
