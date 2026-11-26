import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from orchd import bindings, store
from orchd.channels import repository
from tests.test_snapshot import fingerprints


class BindingInventoryTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.path = self.root / "orchd.db"
        self.con = store.connect(self.path)
        self.addCleanup(self.con.close)
        for orch_id in ("first", "second"):
            store.register_orch(self.con, orch_id, "claude")
        self.con.execute("INSERT INTO entries(id,orch_id,thread_id,bound_at) VALUES('desktop','first','chat-one',1)")
        self.con.execute(
            "INSERT INTO interfaces(id,name,orch_id,token_hash,expires_at,idle_seconds,disabled) "
            "VALUES('legacy','kc-slack:example','first','private-fixture-token',9999999999,600,0)"
        )

    def cli(self, *args):
        env = {**os.environ, "ORCHD_HOME": str(self.root), "ORCHD_ROOT": str(self.root / "orch")}
        return subprocess.run([sys.executable, "-m", "orchd.cli", "bindings", *args], env=env,
                              capture_output=True, encoding="utf-8", timeout=20)

    def test_legacy_db_remains_read_only_and_native_routes_are_unavailable(self):
        before = fingerprints(self.path)
        result = self.cli("--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertFalse(report["native_channels_available"])
        first, second = report["orchs"]
        self.assertEqual(first["desktop"], [{"entry_id": "desktop", "thread_id": "chat-one"}])
        self.assertEqual(first["listeners"], [{"name": "kc-slack:example", "state": "registered"}])
        self.assertFalse(second["desktop"])
        self.assertNotIn("private-fixture-token", result.stdout)
        self.assertEqual(fingerprints(self.path), before)
        self.assertFalse((self.root / "orch").exists())

    def test_filter_and_render_native_channel_bindings_by_owner(self):
        with repository.connect(self.path) as channels:
            self.addCleanup(channels.close)
            channels.execute(
                "INSERT INTO channel_sources(id,host_id,provider,account_ref,adapter_id,enabled,manifest_hash,created_at) "
                "VALUES('line-source','host','line','account','line',1,'hash','now')"
            )
            channels.execute(
                "INSERT INTO channel_routes(id,version,scope_key,source_id,conversation_ref,project_ref,"
                "owner_orch,mode,authority_epoch,effective_at,manifest_hash) "
                "VALUES('line-route',1,'scope','line-source','group-1','project-a','second','receive_only',1,'now','hash')"
            )
            report = bindings.read(self.path, "second")
            self.assertTrue(report["native_channels_available"])
            self.assertEqual([item["id"] for item in report["orchs"]], ["second"])
            self.assertEqual(report["orchs"][0]["channels"][0]["conversation_ref"], "group-1")
            result = self.cli("--orch", "second")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Channel line line-source/group-1", result.stdout)
            self.assertNotIn("chat-one", result.stdout)
            self.assertNotEqual(self.cli("--orch", "missing").returncode, 0)
