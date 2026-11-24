import json
import os
import shutil
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

    def entry(self, arguments=None, *, name="dashboard", entry_id="desktop"):
        with patch.dict(os.environ, self.env):
            response = mcp_server.handle({"id": 1, "method": "tools/call", "params": {
                "name": name, "arguments": arguments or {},
            }}, None, None, role="entry", entry_id=entry_id)
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


    def test_dashboard_actions_only_use_the_local_desktop_binding(self):
        for name, action in (("dashboard_read", "read"), ("dashboard_refresh", "refresh")):
            with self.subTest(name=name), patch("orchd.dashboard.run_action", return_value={"page_url": PAGE}) as run:
                self.assertTrue(self.entry({"page_url": PAGE}, name=name)["isError"])
                self.assertTrue(self.entry(name=name, entry_id="other")["isError"])
                run.assert_not_called()
                self.assertEqual(self.entry(name=name)["structuredContent"], {"page_url": PAGE})
                run.assert_called_once_with(action)

    def test_missing_pages_publisher_keeps_status_available(self):
        self.write(self.config / "dashboard.json", {"page_url": PAGE})
        for name in ("dashboard_read", "dashboard_refresh"):
            with self.subTest(name=name):
                result = self.entry(name=name)
                self.assertTrue(result["isError"])
                self.assertIn("Pages publisher is not configured", result["content"][0]["text"])
        self.assertEqual(self.entry()["structuredContent"]["status"], "never_synced")


    @unittest.skipUnless(shutil.which("node"), "Node.js is required for the Pages publisher")
    def test_overlapping_publisher_keeps_newer_page_content(self):
        fixture = Path(__file__).with_name("fixtures") / "dashboard_publisher_race.cjs"
        result = subprocess.run(["node", str(fixture)], capture_output=True, encoding="utf-8", timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


    def test_refresh_rejects_uncoordinated_periodic_publisher(self):
        self.write(self.config / "dashboard.json", {"page_url": PAGE})
        publisher = self.config / "pages-publisher"
        self.write(publisher / "config.json", {})
        (publisher / "orch-dash-pages-sync.js").write_text("// obsolete publisher", encoding="utf-8")
        result = self.entry(name="dashboard_refresh")
        self.assertTrue(result["isError"])
        self.assertIn("Update the periodic Dashboard publisher", result["content"][0]["text"])
