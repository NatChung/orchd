import contextlib
import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchd import cli, goals, snapshot, store


def fingerprints(path):
    return {suffix: hashlib.sha256(Path(str(path) + suffix).read_bytes()).hexdigest()
            for suffix in ("", "-wal", "-shm", "-journal") if Path(str(path) + suffix).exists()}


def seed(con):
    """Only synthetic data; also used to generate the review example."""
    goal = goals.set_goal(con, actor="synthetic-operator", repo="example", pg="Synthetic goal")
    for tid, status in (("open", "done"), ("closed", "closed")):
        con.execute("INSERT INTO tasks(id,repo,repo_path,title,instructions,done_when,orch_thread,codex_bin,"
                    "status,created_at,updated_at,goal_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (tid, "example", "/synthetic/repo", "Synthetic task", "Synthetic instructions", "Synthetic check",
                     "orch-current", "codex", status, 1, 2, goal["id"]))
    con.execute("INSERT INTO entries(id,orch_id,bound_at) VALUES('synthetic-entry','orch-current',1)")
    for mid, orch, task, state, delivery in (
            (1, "orch-current", "open", "current", "delivered"),
            (2, "orch-current", "open", "queued", "held"),
            (3, "orch-old", "closed", "current", "delivered"),
            (4, "orch-current", "open", "answered", "delivered"),
            (5, "orch-current", None, "answered", "delivered")):
        con.execute("INSERT INTO entry_messages(id,entry_id,orch_id,direction,kind,body,body_bytes,body_sha256,"
                    "task_id,source_message_id,question_state,delivery,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (mid, "synthetic-entry", orch, "out", "question", "Synthetic question", 18, "synthetic-hash",
                     task, 1, state, delivery, mid))
    con.execute("INSERT INTO entry_messages(entry_id,orch_id,direction,kind,body,body_bytes,body_sha256,"
                "task_id,reply_to,delivery,created_at) VALUES('synthetic-entry','orch-current','in','reply',"
                "'Synthetic reply',15,'synthetic-hash','open',4,'sending',6)")
    for kind, read_at in (("progress", None), ("report", None), ("question", None), ("question", 7),
                          ("answer_queued", None), ("answer_queued", 8), ("answer", None)):
        store.add_message(con, "open", kind, "Synthetic worker message", "Synthetic evidence")
        con.execute("UPDATE messages SET read_at=? WHERE id=last_insert_rowid()", (read_at,))


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "orchd.db"
        self.con = store.connect(self.path)
        self.addCleanup(self.con.close)
        seed(self.con)

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, ORCHD_HOME=self.temp.name), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["snapshot", "--json", *args])
        return code, out.getvalue(), err.getvalue()

    def assert_cli_success(self, code, err):
        self.assertEqual(code, 0, err)
        self.assertFalse(any(line.startswith("orchd snapshot:") for line in err.splitlines()), err)

    def test_cli_structure_and_source_db_wal_bytes_unchanged(self):
        before = fingerprints(self.path)
        self.assertIn("-wal", before)
        with patch("orchd.store.connect", side_effect=AssertionError("must not migrate")), \
                patch("orchd.cli.Runtime", side_effect=AssertionError("must not probe or deliver")):
            code, out, err = self.run_cli()
        self.assert_cli_success(code, err)
        self.assertEqual(fingerprints(self.path), before)
        data = json.loads(out)
        self.assertEqual(data["source_db"], str(self.path.resolve()))
        self.assertEqual(data["schema_version"], 1)
        self.assertEqual(len(data["schema_fingerprint"]), 64)
        self.assertGreater(data["generated_at"], 0)
        self.assertEqual(len(data["goals"][0]["history"]), 1)
        self.assertEqual([t["id"] for t in data["tasks"]], ["open"])
        self.assertEqual(data["tasks"][0]["status"], "done")
        self.assertEqual(data["tasks"][0]["latest_report"]["kind"], "report")
        self.assertNotIn("body", data["tasks"][0]["latest_progress"])

    def test_old_binding_closed_task_and_held_remain_identifiable(self):
        data = snapshot.read(self.path)
        questions = {q["id"]: q for q in data["entry_messages"]}
        self.assertTrue(questions[1]["binding_current"])
        self.assertTrue(questions[1]["task_open"])
        self.assertEqual(questions[2]["delivery"], "held")
        self.assertFalse(questions[3]["binding_current"])
        self.assertFalse(questions[3]["task_open"])
        self.assertEqual(questions[3]["task"]["repo"], "example")
        self.assertEqual(questions[3]["body"], "Synthetic question")
        self.assertIsNone(questions[5]["task_open"])
        self.assertIsNone(questions[5]["task"])

    def test_worker_questions_are_unread_orch_triage_not_nat_questions(self):
        data = snapshot.read(self.path)
        self.assertEqual(len(data["worker_questions"]), 1)
        question = data["worker_questions"][0]
        self.assertEqual(question["audience"], "orch")
        self.assertEqual(question["processing"], "awaiting_orch_triage")
        self.assertTrue(all(q["audience"] == "nat" for q in data["entry_messages"]))

    def test_reply_stages_and_task_scoped_worker_evidence(self):
        for delivery, expected in (("pending", "pending"), ("failed", "failed"),
                                   ("sending", "uncertain"), ("delivered", "notified"), ("other", "unknown")):
            self.con.execute("UPDATE entry_messages SET delivery=?,read_at=9,delivered_at=8,recipient='orch-current' "
                             "WHERE kind='reply'", (delivery,))
            data = snapshot.read(self.path)
            reply = data["replies"][0]
            self.assertEqual(reply["processing"]["orch_notification"], expected)
            self.assertEqual(reply["read_at"], 9)
            self.assertEqual(reply["delivered_at"], 8)
            self.assertEqual(reply["recipient"], "orch-current")
            self.assertTrue(reply["processing"]["saved"])
            for field in ("worker_forwarded", "worker_confirmed", "worker_executed"):
                self.assertEqual(reply["processing"][field], "unknown")
            self.assertEqual([a["processing"] for a in data["worker_answers"]],
                             ["pending", "transport_accepted", "transport_accepted"])
            self.assertTrue(all(a["reply_id"] is None for a in data["worker_answers"]))

    def test_recent_answered_limit_never_limits_pending(self):
        for limit, expected in ((0, [1, 2, 3]), (1, [1, 2, 3, 5])):
            code, out, err = self.run_cli("--answered-limit", str(limit))
            self.assert_cli_success(code, err)
            self.assertEqual([q["id"] for q in json.loads(out)["entry_messages"]], expected)
        self.assertNotEqual(self.run_cli("--answered-limit", "-1")[0], 0)

    def test_missing_corrupt_or_incomplete_db_fails_without_empty_json(self):
        self.con.close()
        self.path.unlink()
        for content in (None, b"invalid sqlite", b""):
            if content is not None:
                self.path.write_bytes(content)
            before = fingerprints(self.path)
            code, out, err = self.run_cli()
            self.assertNotEqual(code, 0)
            self.assertEqual(out, "")
            self.assertIn("orchd snapshot:", err)
            self.assertEqual(fingerprints(self.path), before)

    def test_process_exit_code_on_read_failure(self):
        with tempfile.TemporaryDirectory() as missing:
            result = subprocess.run(
                [sys.executable, "-c", "from orchd.cli import main; import sys; sys.exit(main())",
                 "snapshot", "--json"], env={**os.environ, "ORCHD_HOME": missing},
                text=True, capture_output=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "")
            self.assertIn("orchd snapshot:", result.stderr)
            self.assertFalse((Path(missing) / "orchd.db").exists())

    def test_incomplete_sqlite_schema_fails_without_migration(self):
        self.con.close()
        self.path.unlink()
        with sqlite3.connect(self.path) as con:
            con.execute("CREATE TABLE tasks(id TEXT PRIMARY KEY)")
        con.close()
        before = fingerprints(self.path)
        code, out, err = self.run_cli()
        self.assertNotEqual(code, 0)
        self.assertEqual(out, "")
        self.assertIn("orchd snapshot:", err)
        self.assertIn("no such column", err)
        self.assertEqual(fingerprints(self.path), before)

    def test_success_tolerates_unrelated_resource_warning_on_stderr(self):
        main = cli.main

        def unrelated_warning_then_main(args):
            print("ResourceWarning: unclosed database from another test", file=sys.stderr)
            return main(args)

        with patch("orchd.cli.main", side_effect=unrelated_warning_then_main):
            code, out, err = self.run_cli()
        self.assert_cli_success(code, err)
        self.assertIn("ResourceWarning:", err)
        self.assertEqual(json.loads(out)["schema_version"], 1)
