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

from orchd import entry_ops, mcp_server, store, upgrade_probe


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

    def create_pending(self):
        rt = SimpleNamespace(send_uds=lambda *a: self.sent.append(a))
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
        result = upgrade_probe.verify(self.con, SimpleNamespace(send_uds=send), self.op, timeout=0)
        self.assertEqual(result["state"], "verified")
        self.assertEqual(result["session_id"], "new-session")
        self.assertEqual(self.con.execute("SELECT count(*) FROM tasks").fetchone()[0], 0)
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
        result = upgrade_probe.verify(self.con, SimpleNamespace(send_uds=uncertain), self.op, timeout=0)
        self.assertEqual(result["reason"], "probe_delivery_unknown")
        self.create_pending()
        self.assertEqual(len(self.sent), 1)

    def test_fresh_cli_finishes_the_existing_pending_probe(self):
        self.create_pending()
        self.assertFalse(self.ack().get("isError"))
        record = entry_ops.read(self.op)
        record["state"] = "needs_session_refresh"
        entry_ops._write(record)
        result = subprocess.run([sys.executable, "-B", "-m", "orchd.cli", "_entry-verify", self.op],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["state"], "verified")
        self.assertEqual(entry_ops.public(entry_ops.read(self.op))["state"], "done")
        self.assertEqual(len(self.sent), 1)
