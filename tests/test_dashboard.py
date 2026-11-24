import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from orchd import mcp_server


PAGE = "https://chatgpt.com/space/page_test_dashboard"
ATTEMPT = "2026-11-24T10:10:00Z"
SUCCESS = "2026-11-24T10:00:00Z"


class DashboardTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name).resolve()
        self.config = root / "config"
        self.state = root / "state"
        self.env = {"ORCHD_HOME": str(self.state), "ORCHD_CONFIG_DIR": str(self.config),
                    "ORCHD_ROOT": str(root / "orch")}

    def write(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")

    def cli(self, *args):
        return subprocess.run([sys.executable, "-m", "orchd.cli", "dashboard", *args],
                              env={**os.environ, **self.env}, capture_output=True, text=True, timeout=20)

    def entry(self, arguments=None):
        with patch.dict(os.environ, self.env):
            response = mcp_server.handle({"id": 1, "method": "tools/call", "params": {
                "name": "dashboard", "arguments": arguments or {},
            }}, None, None, role="entry")
        return response["result"]

    def test_unconfigured_lookup_creates_no_state(self):
        result = self.cli("--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {
            "page_url": None, "status": "unconfigured", "last_attempt_at": None, "last_synced_at": None,
        })
        self.assertFalse(self.state.exists())

    def test_cli_and_interface_read_the_same_sync_receipt_without_database(self):
        self.write(self.config / "dashboard.json", {"page_url": PAGE})
        receipt = self.state / "dashboard-sync.json"
        self.write(receipt, {"page_url": PAGE, "status": "failed",
                             "last_attempt_at": ATTEMPT, "last_synced_at": SUCCESS})
        initial = receipt.read_bytes()
        result = self.cli("--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        expected = {"page_url": PAGE, "status": "failed", "last_attempt_at": ATTEMPT, "last_synced_at": SUCCESS}
        self.assertEqual(json.loads(result.stdout), expected)
        self.assertEqual(self.entry()["structuredContent"], expected)
        self.assertIn(PAGE, self.cli().stdout)
        self.assertEqual(receipt.read_bytes(), initial)
        self.assertFalse((self.state / "orchd.db").exists())

    def test_saved_receipt_for_a_different_page_is_ignored(self):
        self.write(self.config / "dashboard.json", {"page_url": PAGE})
        self.write(self.state / "dashboard-sync.json", {"page_url": PAGE + "_old", "status": "synced",
                                                       "last_attempt_at": SUCCESS, "last_synced_at": SUCCESS})
        self.assertEqual(self.entry()["structuredContent"]["status"], "never_synced")

    def test_non_page_link_and_inconsistent_receipt_are_rejected(self):
        self.write(self.config / "dashboard.json", {"page_url": "https://chatgpt.com.evil.test/space/page_a"})
        self.assertEqual(self.cli("--json").returncode, 1)
        self.write(self.config / "dashboard.json", {"page_url": PAGE})
        self.write(self.state / "dashboard-sync.json", {"page_url": PAGE, "status": "synced",
                                                       "last_attempt_at": SUCCESS, "last_synced_at": ATTEMPT})
        self.assertTrue(self.entry()["isError"])

    def test_entry_accepts_no_destination_override(self):
        self.assertTrue(self.entry({"page_url": PAGE})["isError"])
