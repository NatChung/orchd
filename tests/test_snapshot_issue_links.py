import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchd import issue_links, snapshot, store
from test_snapshot import fingerprints, seed


class SnapshotIssueLinkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "orchd.db"
        self.con = store.connect(self.path)
        self.addCleanup(self.con.close)
        seed(self.con)

    def test_unlinked_legacy_messages_schema_available_without_migration(self):
        self.assertNotIn("issue_ref", [r["name"] for r in self.con.execute("PRAGMA table_info(tasks)")])
        before = fingerprints(self.path)
        with patch("orchd.store.connect", side_effect=AssertionError("no migration")):
            result = snapshot.read(self.path, schema_version=2)
        self.assertEqual(result["issue_links"], dict(version=1, available=True, history=[]))
        self.assertEqual(result["tasks"][0]["issue_ref"], None)
        self.assertEqual(fingerprints(self.path), before)

    def test_current_ref_all_embedded_tasks_closed_lookup_and_history(self):
        first = issue_links.link(self.con, "open", "github:example/project#1")
        second = issue_links.link(self.con, "open", "jira:TEST-2")
        issue_links.link(self.con, "closed", "github:example/project#3")
        before = fingerprints(self.path)
        result = snapshot.read(self.path, schema_version=2)
        self.assertEqual(result["tasks"][0]["issue_ref"], "jira:TEST-2")
        for record in result["entry_messages"] + result["worker_questions"] + result["pending_worker_questions"]:
            if record["task"]:
                task = record["task"]
                self.assertEqual(task["issue_ref"], "jira:TEST-2" if task["id"] == "open" else "github:example/project#3")
        for goal in result["goals"]:
            for task in goal["linked_tasks"]:
                self.assertEqual(task["issue_ref"], "jira:TEST-2" if task["task_id"] == "open" else "github:example/project#3")
        history = result["issue_links"]["history"]
        self.assertEqual(history[1]["supersedes"], first["message_id"])
        self.assertEqual(history[1]["message_id"], second["message_id"])
        self.assertEqual(fingerprints(self.path), before)
        issue_links.unlink(self.con, "open")
        self.assertEqual(snapshot.read(self.path, schema_version=2)["tasks"][0]["issue_ref"], None)

    def test_v1_matches_frozen_reader_even_with_link_events(self):
        issue_links.link(self.con, "open", "jira:TEST-1")
        fixture = Path(__file__).parent / "fixtures" / "snapshot_v1.py"
        spec = importlib.util.spec_from_file_location("orchd._snapshot_v1_fixture", fixture)
        original = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(original)
        with patch("time.time", return_value=123456):
            expected = original.read(self.path)
            self.assertEqual(snapshot.read(self.path), expected)
            self.assertEqual(snapshot.read(self.path, schema_version=1), expected)
        self.assertNotIn("issue_links", expected)
        self.assertNotIn("issue_ref", expected["tasks"][0])

    def test_no_prose_inference_or_unrelated_messages(self):
        store.add_message(self.con, "open", "progress", "github:example/project#99")
        store.add_message(self.con, "open", issue_links.KIND, "not json")
        store.add_message(self.con, "open", issue_links.KIND, '{"version":99,"ref":"jira:TEST-99"}')
        self.assertEqual(snapshot.read(self.path, schema_version=2)["tasks"][0]["issue_ref"], None)
