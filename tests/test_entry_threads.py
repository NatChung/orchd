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


class QuestionThreadTest(ThreadTest):
    def task(self):
        return core.dispatch(self.con, self.rt, orch_thread=self.orch, repo="demo", title="T", instructions="i",
                             done_when="d", model="sonnet", model_reason="r", task_type="outward")["id"]

    def answer(self, question_id, text="answer"):
        self.rt.nat_says("desk", text)
        return entry.relay(self.con, self.rt, "desktop", "desk", reply_to=question_id)["message_id"]

    def run_cli(self, args):
        out, err = io.StringIO(), io.StringIO()
        env = {"ORCHD_HOME": self.tmp.name, "ORCHD_ORCH_ID": self.orch}
        with patch.dict(os.environ, env), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.main(["entry", "thread"] + args)
            except SystemExit as exit_:
                code = exit_.code
        return code, out.getvalue(), err.getvalue()

    def snapshot_files(self, checkpoint=False):
        if checkpoint:
            self.con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return {str(p): p.read_bytes() for p in Path(self.tmp.name).glob("orchd.db*") if p.is_file()}

    def test_contract_full_text_and_reply_without_task_id(self):
        task = self.task()
        long = "問" * 450 + "?"
        q = entry.ask_nat(self.con, self.rt, self.orch, long, task, issue_ref=REF)["question_id"]
        reply = self.answer(q, "答" * 300)
        self.con.execute("UPDATE entry_messages SET task_id=NULL WHERE id=?", (reply,))  # direct input carries no task_id
        before = self.snapshot_files(checkpoint=True)
        data = entry_threads.read_question(self.path, q)
        self.assertEqual(self.snapshot_files(), before)
        self.assertEqual((data["schema_version"], data["status"]), (1, "ok"))
        self.assertTrue(data["read_at"].endswith("Z"))
        thread = data["threads"][0]
        for key in ("question_id", "task_id", "issue_ref", "question_state", "revisit_at", "status", "root_question",
                    "messages", "complete", "has_more", "next_cursor"):
            self.assertIn(key, thread)
        self.assertEqual((thread["question_id"], thread["task_id"], thread["issue_ref"]), (q, task, REF))
        self.assertEqual(thread["status"], "waiting_orch")
        self.assertEqual(thread["root_question"], dict(id=q, body=long))
        self.assertEqual([m["id"] for m in thread["messages"]], [reply, q])
        self.assertEqual(thread["messages"][0]["text"], "答" * 300)
        self.assertEqual(thread["messages"][1]["text"], long)
        self.assertEqual((thread["messages"][0]["reply_to"], thread["messages"][0]["who"]), (q, "nat"))
        self.assertTrue(thread["complete"] and not thread["has_more"] and thread["next_cursor"] is None)

    def test_same_task_questions_are_isolated(self):
        task = self.task()
        first = entry.ask_nat(self.con, self.rt, self.orch, "first", task)["question_id"]
        r1 = self.answer(first, "a1")
        second = entry.ask_nat(self.con, self.rt, self.orch, "second", task)["question_id"]
        r2 = self.answer(second, "a2")
        by_q = {t["question_id"]: [m["id"] for m in t["messages"]]
                for t in entry_threads.read_task(self.path, task)["threads"]}
        self.assertEqual(by_q, {first: [r1, first], second: [r2, second]})
        self.assertEqual([t["question_id"] for t in entry_threads.read_task(self.path, task)["threads"]], [second, first])

    def test_superseded_chain_visible_from_new_and_old_question(self):
        task = self.task()
        old = entry.ask_nat(self.con, self.rt, self.orch, "v1", task)["question_id"]
        old_reply = self.answer(old, "old answer")
        new = entry.ask_nat(self.con, self.rt, self.orch, "v2", task, supersedes=old, expected_version=1,
                            expected_state="answered", reason="changed")["question_id"]
        for selector in (new, old):
            thread = entry_threads.read_question(self.path, selector, limit=10)["threads"][0]
            self.assertEqual((thread["question_id"], thread["question_chain"]), (new, [old, new]))
            self.assertEqual(thread["root_question"]["id"], old)
            msgs = {m["id"]: m for m in thread["messages"]}
            self.assertEqual(set(msgs), {old, old_reply, new})
            self.assertEqual((msgs[new]["supersedes"], msgs[old]["superseded_by"]), (old, new))
        self.assertEqual(len(entry_threads.read_task(self.path, task)["threads"]), 1)

    def test_not_found_error_and_missing_history_are_distinct(self):
        self.assertEqual(entry_threads.read_question(self.path, 999)["status"], "not_found")
        self.assertEqual(entry_threads.read_task(self.path, "nope")["status"], "not_found")
        self.assertEqual(self.run_cli(["--question-id", "999"])[0], 3)
        self.assertEqual(self.run_cli(["--task-id", "nope"])[0], 3)
        missing = Path(self.tmp.name) / "missing"
        with patch.dict(os.environ, {"ORCHD_HOME": str(missing)}), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["entry", "thread", "--question-id", "1"]), 4)
        task = self.task()
        old = entry.ask_nat(self.con, self.rt, self.orch, "v1", task)["question_id"]
        new = entry.ask_nat(self.con, self.rt, self.orch, "v2", task, supersedes=old, expected_version=1,
                            expected_state="current", reason="r")["question_id"]
        self.con.execute("PRAGMA foreign_keys=OFF")
        self.con.execute("DELETE FROM entry_messages WHERE id=?", (old,))
        data = entry_threads.read_question(self.path, new)
        self.assertEqual(data["status"], "partial")
        self.assertEqual((data["threads"][0]["missing_history"], data["threads"][0]["complete"]), ([old], False))
        self.assertEqual(self.run_cli(["--question-id", str(new)])[0], 5)

    def test_more_than_100_answered_pages_with_cursor_and_tiebreak(self):
        task = self.task()
        target = entry.ask_nat(self.con, self.rt, self.orch, "target", task)["question_id"]
        base = "INSERT INTO entry_messages(entry_id,orch_id,direction,kind,body,body_bytes,body_sha256,delivery,created_at,reply_to,task_id) "
        for i in range(120):
            self.con.execute(base + "VALUES('desktop',?, 'in','reply',?,1,'h','delivered',?,?,?)",
                             (self.orch, f"r{i}", 1000 + i // 2, target, task))  # pairs share a timestamp
        for i in range(105):  # unrelated answered noise past the snapshot's 100-row cap
            entry.ask_nat(self.con, self.rt, self.orch, f"noise{i}", None)
        self.con.execute("UPDATE entry_messages SET created_at=1 WHERE id=?", (target,))
        seen, cursor, pages = [], None, 0
        while True:
            thread = entry_threads.read_question(self.path, target, limit=50, cursor=cursor)["threads"][0]
            seen += [m["id"] for m in thread["messages"]]
            pages += 1
            cursor = thread["next_cursor"]
            self.assertEqual(thread["has_more"], cursor is not None)
            if cursor is None:
                break
        self.assertEqual((pages, len(seen), seen[-1]), (3, 121, target))
        self.assertEqual(seen[:-1], sorted(seen[:-1], reverse=True))  # same created_at: id descending
        first = entry_threads.read_question(self.path, target, limit=3)["threads"][0]
        self.assertEqual(len(first["messages"]), 3)
        self.assertTrue(first["has_more"])
        with self.assertRaises(entry_threads.CursorError):
            entry_threads.read_question(self.path, target, cursor="garbage")
        for limit in (0, 101):
            with self.assertRaises(entry_threads.CursorError):
                entry_threads.read_question(self.path, target, limit=limit)

    def test_task_thread_list_pages(self):
        task = self.task()
        qs = [entry.ask_nat(self.con, self.rt, self.orch, f"q{i}", task)["question_id"] for i in range(3)]
        page = entry_threads.read_task(self.path, task, thread_limit=2)
        self.assertEqual(([t["question_id"] for t in page["threads"]], page["has_more"]), (qs[:0:-1], True))
        rest = entry_threads.read_task(self.path, task, thread_limit=2, cursor=page["next_cursor"])
        self.assertEqual(([t["question_id"] for t in rest["threads"]], rest["has_more"], rest["next_cursor"]), ([qs[0]], False, None))

    def test_cli_contract_selectors_and_readonly(self):
        task = self.task()
        q = entry.ask_nat(self.con, self.rt, self.orch, "cli", task)["question_id"]
        before = self.snapshot_files(checkpoint=True)
        code, out, _ = self.run_cli(["--question-id", str(q), "--limit", "1", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["threads"][0]["messages"][0]["text"], "cli")
        code, out, _ = self.run_cli(["--task-id", task, "--per-question-limit", "1"])
        self.assertEqual((code, json.loads(out)["task_id"]), (0, task))
        self.assertEqual(self.snapshot_files(), before)
        self.assertEqual(self.run_cli(["--question-id", "1", "--task-id", "x"])[0], 2)
        self.assertEqual(self.run_cli(["--question-id", "1", "--issue-ref", REF])[0], 2)
        self.assertEqual(self.run_cli(["--question-id", "1", "--per-question-limit", "2"])[0], 2)
        self.assertEqual(self.run_cli(["--task-id", "x", "--limit", "5"])[0], 2)
        self.assertEqual(self.run_cli(["--question-id", str(q), "--cursor", "bad"])[0], 2)
        self.assertEqual(self.run_cli(["--question-id", str(q), "--limit", "0"])[0], 2)

    def test_issue_refs_output_unchanged(self):
        sent = entry.send_to_nat(self.con, self.rt, self.orch, "中" * 201, issue_ref=REF)["message_id"]
        self.con.execute("UPDATE entry_messages SET created_at=100")
        code, out, _ = self.run_cli(["--issue-refs", REF, "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), dict(schema_version=1, entry_id="desktop", threads={REF: dict(
            status="waiting_nat", messages=[dict(message_id=sent, at=100.0, who="orch", direction="orch→nat", kind="message",
                                                 text="中" * 200, full_bytes=603)])}))


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
