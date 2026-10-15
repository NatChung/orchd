"""Issue-linked entry messages; synthetic fixtures, never the installed DB."""
import contextlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchd import cli, core, entry, entry_threads, issue_links, mcp_server, store
from tests.test_entry import EntryRuntime

REF = "github:example/project#40"
OTHER = "jira:DEMO-37"


class ThreadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "orchd.db"
        self.con = store.connect(self.path)
        self.addCleanup(self.con.close)
        self.rt = EntryRuntime()
        self.orch = core.start_orch(self.con, self.rt, "opus")["id"]
        entry.bind(self.con, self.rt, self.orch)
        entry.touch(self.con, "desktop", "desk")

    def read(self, refs=(REF,), **kwargs):
        return entry_threads.read(self.path, refs, **kwargs)["threads"]

    def call_cli(self, args, *, identity=None):
        out = io.StringIO()
        env = {"ORCHD_HOME": self.tmp.name, "ORCHD_ORCH_ID": identity or self.orch}
        with patch.dict(os.environ, env), patch("orchd.cli.Runtime", return_value=self.rt), contextlib.redirect_stdout(out):
            code = cli.main(args)
        self.assertEqual(code, 0)
        return json.loads(out.getvalue())

    def test_send_ask_reply_and_legacy_messages(self):
        plain = entry.send_to_nat(self.con, self.rt, self.orch, "untagged")
        sent = entry.send_to_nat(self.con, self.rt, self.orch, "update", issue_ref=REF)
        question = entry.ask_nat(self.con, self.rt, self.orch, "question", issue_ref=REF)
        self.assertEqual(self.read()[REF]["status"], "waiting_nat")
        self.rt.nat_says("desk", "answer")
        reply = entry.relay(self.con, self.rt, "desktop", "desk", reply_to=question["question_id"])
        thread = self.read()[REF]
        self.assertEqual(thread["status"], "waiting_orch")
        self.assertEqual([m["message_id"] for m in thread["messages"]],
                         [reply["message_id"], question["question_id"], sent["message_id"]])
        self.assertEqual(thread["messages"][0]["who"], "nat")
        self.assertIsNone(entry._row(self.con, plain["message_id"])["issue_ref"])
        self.assertEqual(entry._row(self.con, reply["message_id"])["issue_ref"], REF)

    def test_task_defaults_override_and_unlink(self):
        task = core.dispatch(self.con, self.rt, orch_thread=self.orch, repo="demo", title="T", instructions="i",
                             done_when="d", model="sonnet", model_reason="r", task_type="outward")["id"]
        issue_links.link(self.con, task, REF)
        q = entry.ask_nat(self.con, self.rt, self.orch, "default", task)
        self.assertEqual(entry._row(self.con, q["question_id"])["issue_ref"], REF)
        q = entry.ask_nat(self.con, self.rt, self.orch, "override", task, issue_ref=OTHER)
        self.assertEqual(entry._row(self.con, q["question_id"])["issue_ref"], OTHER)
        issue_links.unlink(self.con, task)
        q = entry.ask_nat(self.con, self.rt, self.orch, "unlinked", task)
        self.assertIsNone(entry._row(self.con, q["question_id"])["issue_ref"])

    def test_invalid_refs_rejected_before_writes(self):
        for ref in ("bad", "github:o/r#0", "jira:bad-1", [REF, OTHER], ""):
            for fn in (entry.send_to_nat, entry.ask_nat):
                with self.subTest(ref=ref, fn=fn), self.assertRaises(ValueError):
                    fn(self.con, self.rt, self.orch, "invalid", issue_ref=ref)
        self.assertEqual(self.con.execute("SELECT count(*) FROM entry_messages").fetchone()[0], 0)

    def test_tag_clear_audit_idempotence_and_binding_permissions(self):
        msg = entry.send_to_nat(self.con, self.rt, self.orch, "tag me")["message_id"]
        self.assertTrue(entry.entry_tag(self.con, self.orch, msg, REF)["changed"])
        self.assertFalse(entry.entry_tag(self.con, self.orch, msg, REF)["changed"])
        self.assertEqual(self.read()[REF]["messages"][0]["message_id"], msg)
        self.assertTrue(entry.entry_tag(self.con, self.orch, msg, None)["changed"])
        self.assertEqual(self.read()[REF]["status"], "none")
        self.assertEqual(self.con.execute("SELECT old_ref,issue_ref FROM entry_message_tags ORDER BY id").fetchall()[1][0], REF)
        self.assertEqual(self.con.execute("SELECT count(*) FROM entry_message_tags").fetchone()[0], 2)
        with self.assertRaises(ValueError):
            entry.entry_tag(self.con, self.orch, 999999, REF)
        with self.assertRaises(ValueError):
            entry.entry_tag(self.con, self.orch, msg, "invalid")
        other_orch = core.start_orch(self.con, self.rt, "opus")["id"]
        entry.bind(self.con, self.rt, other_orch, "private")
        with self.assertRaises(ValueError):
            entry.entry_tag(self.con, other_orch, msg, REF)
        entry.bind(self.con, self.rt, other_orch, force=True)
        with self.assertRaises(ValueError):
            entry.entry_tag(self.con, other_orch, msg, REF)
        with self.assertRaises(ValueError):
            entry.entry_tag(self.con, self.orch, msg, REF)

    def test_thread_batch_order_limits_truncation_privacy_read_only(self):
        ids = [entry.send_to_nat(self.con, self.rt, self.orch, "中" * 201, issue_ref=REF)["message_id"] for _ in range(4)]
        self.con.execute("UPDATE entry_messages SET created_at=100")
        other_orch = core.start_orch(self.con, self.rt, "opus")["id"]
        entry.bind(self.con, self.rt, other_orch, "private")
        private = entry.send_to_nat(self.con, self.rt, other_orch, "private", issue_ref=REF)["message_id"]
        self.con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        before = {str(p): p.read_bytes() for p in Path(self.tmp.name).glob("orchd.db*") if p.is_file()}
        data = self.read((REF, OTHER))
        after = {str(p): p.read_bytes() for p in Path(self.tmp.name).glob("orchd.db*") if p.is_file()}
        self.assertEqual(before, after)
        messages = data[REF]["messages"]
        self.assertEqual([m["message_id"] for m in messages], ids[:0:-1])
        self.assertEqual(messages[0]["text"], "中" * 200)
        self.assertEqual(messages[0]["full_bytes"], 603)
        self.assertEqual(data[OTHER], dict(status="none", messages=[]))
        self.assertEqual(len(self.read(limit=1)[REF]["messages"]), 1)
        self.assertEqual(self.read(entry_id="private")[REF]["messages"][0]["message_id"], private)
        for limit in (0, -1, 101):
            with self.assertRaises(ValueError):
                self.read(limit=limit)
        with self.assertRaises(ValueError):
            self.read(("invalid",))
        # A WAL-only committed write must also appear in a fresh private copy.
        latest = entry.send_to_nat(self.con, self.rt, self.orch, "wal", issue_ref=REF)["message_id"]
        self.assertEqual(self.read()[REF]["messages"][0]["message_id"], latest)

    def test_cli_and_mcp_interfaces(self):
        sent = self.call_cli(["entry", "send", "--text", "CLI", "--issue-ref", REF])
        self.call_cli(["entry", "ask", "--text", "CLI question", "--issue-ref", REF])
        self.call_cli(["entry", "tag", str(sent["message_id"]), "--issue-ref", OTHER])
        data = self.call_cli(["entry", "thread", "--issue-refs", REF + "," + OTHER, "--limit", "1", "--json"])
        self.assertEqual(set(data["threads"]), {REF, OTHER})
        self.assertEqual(data["threads"][OTHER]["messages"][0]["text"], "CLI")
        self.call_cli(["entry", "tag", str(sent["message_id"]), "--clear"])
        sent = mcp_server.call("send_to_nat", {"text": "MCP", "issue_ref": REF}, self.orch, self.con, self.rt)
        mcp_server.call("ask_nat", {"text": "MCP question", "issue_ref": OTHER}, self.orch, self.con, self.rt)
        mcp_server.call("entry_tag", {"message_id": sent["message_id"], "issue_ref": None}, self.orch, self.con, self.rt)
        self.assertIsNone(entry._row(self.con, sent["message_id"])["issue_ref"])
        schemas = {t["name"]: t["inputSchema"] for t in mcp_server.TOOLS}
        for name in ("send_to_nat", "ask_nat", "entry_tag"):
            self.assertIn("issue_ref", schemas[name]["properties"])


class MigrationTest(unittest.TestCase):
    def test_old_database_is_readable_without_migration_then_adds_nullable_ref(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "old.db"
            con = sqlite3.connect(path)
            con.executescript(store.SCHEMA)
            con.execute("INSERT INTO entry_messages(entry_id,orch_id,direction,kind,body,body_bytes,body_sha256,delivery,created_at) "
                        "VALUES('desktop','old','out','message','legacy',6,'hash','delivered',1)")
            con.commit()
            con.close()
            before = path.read_bytes()
            self.assertEqual(entry_threads.read(path, [REF])["threads"][REF]["status"], "none")
            self.assertEqual(path.read_bytes(), before)
            con = store.connect(path)
            self.assertEqual(tuple(con.execute("SELECT body,issue_ref FROM entry_messages").fetchone()), ("legacy", None))
            con.close()
            con = store.connect(path)
            self.assertEqual(con.execute("SELECT count(*) FROM entry_messages").fetchone()[0], 1)
            con.close()
