"""Issue #44: read-only question/task entry threads; synthetic fixtures, never the installed DB."""
import contextlib
import hashlib
import io
import json
import os
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchd import cli, core, entry, entry_threads, store
from tests.test_entry import EntryRuntime

UTC = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z$")
MESSAGE_KEYS = {"id", "question_id", "task_id", "reply_to", "supersedes", "superseded_by", "direction", "kind",
                "who", "at", "text"}
THREAD_KEYS = {"question_id", "requested_question_id", "task_id", "issue_ref", "question_state", "revisit_at",
               "version", "versions", "status", "root_question", "messages", "complete", "history_issues",
               "has_more", "next_cursor"}


class QuestionThreadTest(unittest.TestCase):
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

    # -- helpers ------------------------------------------------------------------------------------------

    def task(self):
        return core.dispatch(self.con, self.rt, orch_thread=self.orch, repo="demo", title="T", instructions="i",
                             done_when="d", model="sonnet", model_reason="r", task_type="outward")["id"]

    def ask(self, text="Synthetic question?", task=None, **kwargs):
        return entry.ask_nat(self.con, self.rt, self.orch, text, task, **kwargs)["question_id"]

    def answer(self, question, text="Synthetic answer"):
        self.rt.nat_says("desk", text)
        return entry.relay(self.con, self.rt, "desktop", "desk", reply_to=question)["message_id"]

    def raw(self, direction, kind, body, created_at, entry_id="desktop", **fields):
        data = body.encode("utf-8")
        fields.update(entry_id=entry_id, orch_id=self.orch, direction=direction, kind=kind, body=body,
                      body_bytes=len(data), body_sha256=hashlib.sha256(data).hexdigest(),
                      delivery="delivered", created_at=created_at)
        cur = self.con.execute(f"INSERT INTO entry_messages({','.join(fields)}) VALUES({','.join('?' * len(fields))})",
                               tuple(fields.values()))
        return cur.lastrowid

    def read(self, **kwargs):
        payload, code = entry_threads.read_questions(self.path, **kwargs)
        return payload, code

    def ok(self, **kwargs):
        payload, code = self.read(**kwargs)
        self.assertEqual((payload["status"], code), ("ok", 0), payload.get("error"))
        return payload

    def call_cli(self, args, code=0, home=None):
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {"ORCHD_HOME": home or self.tmp.name, "ORCHD_ORCH_ID": self.orch}), \
                patch("orchd.cli.Runtime", return_value=self.rt), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                got = cli.main(args)
            except SystemExit as exit_:
                got = exit_.code
        self.assertEqual(got, code, err.getvalue())
        return json.loads(out.getvalue()) if out.getvalue().strip() else None

    def db_bytes(self):
        self.con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        # -shm is derived index state that the test's own checkpoint rewrites; the DB and WAL hold the data.
        return {p.name: p.read_bytes() for p in Path(self.tmp.name).glob("orchd.db*")
                if p.is_file() and not p.name.endswith("-shm")}

    # -- contract -----------------------------------------------------------------------------------------

    def test_cli_json_contract_fields_and_types(self):
        task = self.task()
        q = self.ask("Which option?", task)
        reply = self.answer(q, "Option B")
        data = self.call_cli(["entry", "thread", "--question-id", str(q), "--limit", "3", "--json"])
        self.assertEqual(data["schema_version"], 1)
        self.assertEqual(data["status"], "ok")
        self.assertTrue(data["complete"])
        self.assertRegex(data["read_at"], UTC)
        self.assertEqual(data["entry_id"], "desktop")
        self.assertEqual(data["selector"], dict(type="question", question_id=q, limit=3, cursor=None))
        thread, = data["threads"]
        self.assertEqual(set(thread), THREAD_KEYS)
        self.assertEqual((thread["question_id"], thread["requested_question_id"], thread["task_id"]), (q, q, task))
        self.assertEqual((thread["question_state"], thread["status"], thread["version"]), ("answered", "waiting_orch", 1))
        self.assertIsNone(thread["revisit_at"])
        self.assertEqual(thread["root_question"]["id"], q)
        self.assertEqual(thread["root_question"]["body"], "Which option?")
        self.assertRegex(thread["root_question"]["at"], UTC)
        self.assertEqual((thread["complete"], thread["has_more"], thread["next_cursor"], thread["history_issues"]),
                         (True, False, None, []))
        self.assertEqual([m["id"] for m in thread["messages"]], [reply, q])
        for message in thread["messages"]:
            self.assertEqual(set(message), MESSAGE_KEYS)
            self.assertRegex(message["at"], UTC)
            self.assertIsInstance(message["text"], str)
        answer, question = thread["messages"]
        self.assertEqual((answer["who"], answer["direction"], answer["kind"], answer["reply_to"], answer["question_id"]),
                         ("nat", "nat→orch", "reply", q, q))
        self.assertEqual((question["who"], question["direction"], question["kind"], question["reply_to"]),
                         ("orch", "orch→nat", "question", None))
        self.assertEqual(question["task_id"], task)

        task_data = self.call_cli(["entry", "thread", "--task-id", task, "--per-question-limit", "3", "--json"])
        self.assertEqual(task_data["selector"], dict(type="task", task_id=task, limit=20, per_question_limit=3,
                                                     cursor=None))
        self.assertEqual(task_data["thread_page"], dict(limit=20, returned=1, total=1, has_more=False, next_cursor=None))
        self.assertIsNone(task_data["threads"][0]["requested_question_id"])
        self.assertEqual(task_data["threads"][0]["messages"], thread["messages"])

    def test_selectors_are_exclusive_and_arguments_validated(self):
        q = self.ask()
        task = self.task()
        for args in (["--question-id", str(q), "--task-id", task], ["--question-id", str(q), "--issue-ref", "jira:DEMO-1"],
                     ["--task-id", task, "--issue-refs", "jira:DEMO-1"], [], ["--question-id", "abc"]):
            with self.subTest(args=args):
                self.call_cli(["entry", "thread", *args, "--json"], code=2)
        for args in (["--question-id", str(q), "--per-question-limit", "3"], ["--question-id", str(q), "--limit", "0"],
                     ["--question-id", str(q), "--limit", "101"], ["--question-id", "0"],
                     ["--task-id", task, "--per-question-limit", "0"], ["--task-id", task, "--limit", "101"],
                     ["--question-id", str(q), "--cursor", "not-a-cursor"], ["--task-id", " "]):
            with self.subTest(args=args):
                data = self.call_cli(["entry", "thread", *args, "--json"], code=2)
                self.assertEqual((data["status"], data["complete"], data["threads"]), ("invalid_request", False, []))
                self.assertIn("error", data)
        # The issue-ref selector keeps its exit-1 error style and rejects the new paging flags.
        self.call_cli(["entry", "thread", "--issue-ref", "jira:DEMO-1", "--cursor", "x"], code=1)
        self.call_cli(["entry", "thread", "--issue-ref", "jira:DEMO-1", "--per-question-limit", "3"], code=1)
        payload, code = self.read()
        self.assertEqual((payload["status"], code), ("invalid_request", 2))
        payload, code = self.read(question_id=q, task_id=task)
        self.assertEqual((payload["status"], code), ("invalid_request", 2))

    # -- threading ----------------------------------------------------------------------------------------

    def test_questions_of_one_task_stay_isolated(self):
        task = self.task()
        q1 = self.ask("first?", task)
        q2 = self.ask("second?", task)  # queued behind q1
        r1 = self.answer(q1, "answer one")  # promotes and delivers q2
        r2 = self.answer(q2, "answer two")
        q3 = self.ask("third?", task)
        other = self.ask("other task?", self.task())
        data = self.ok(task_id=task, per_question_limit=10)
        threads = {t["question_id"]: [m["id"] for m in t["messages"]] for t in data["threads"]}
        self.assertEqual(threads, {q1: [r1, q1], q2: [r2, q2], q3: [q3]})
        self.assertEqual([t["question_id"] for t in data["threads"]], [q3, q2, q1])  # latest activity first
        self.assertEqual(data["threads"][0]["status"], "waiting_nat")
        self.assertNotIn(other, threads)
        self.assertEqual([m["id"] for m in self.ok(question_id=q1, limit=10)["threads"][0]["messages"]], [r1, q1])

    def test_superseded_versions_share_one_thread(self):
        task = self.task()
        q1 = self.ask("original?", task)
        r1 = self.answer(q1, "first answer")
        q2 = self.ask("revised?", supersedes=q1, expected_version=1, expected_state="answered", reason="clarify")
        q3 = self.ask("revised again?", supersedes=q2, expected_version=2, expected_state="current", reason="again")
        r3 = self.answer(q3, "final answer")
        for requested in (q1, q2, q3):
            thread, = self.ok(question_id=requested, limit=10)["threads"]
            self.assertEqual((thread["question_id"], thread["requested_question_id"]), (q3, requested))
            self.assertEqual((thread["versions"], thread["version"], thread["question_state"]), ([q1, q2, q3], 3, "answered"))
            self.assertEqual(thread["task_id"], task)  # revisions carry no task; the chain keeps its link
            self.assertEqual(thread["root_question"]["body"], "revised again?")
            messages = {m["id"]: m for m in thread["messages"]}
            self.assertEqual(list(messages), [r3, q3, q2, r1, q1])
            self.assertEqual((messages[q1]["supersedes"], messages[q1]["superseded_by"]), (None, q2))
            self.assertEqual((messages[q2]["supersedes"], messages[q2]["superseded_by"]), (q1, q3))
            self.assertEqual((messages[q3]["supersedes"], messages[q3]["superseded_by"]), (q2, None))
            self.assertEqual(messages[r1]["question_id"], q1)
            self.assertIsNone(messages[q3]["task_id"])
        data = self.ok(task_id=task, per_question_limit=10)
        self.assertEqual([t["question_id"] for t in data["threads"]], [q3])

    def test_reply_without_task_id_joins_its_question_by_reply_to(self):
        task = self.task()
        q = self.ask("needs answer?", task)
        reply = self.raw("in", "reply", "legacy operator reply", 10**10, reply_to=q, task_id=None)
        stray = self.raw("in", "message", "unrelated message", 10**10 + 1)
        thread, = self.ok(task_id=task)["threads"]
        self.assertEqual([m["id"] for m in thread["messages"]], [reply, q])
        self.assertEqual((thread["messages"][0]["task_id"], thread["messages"][0]["question_id"]), (None, q))
        self.assertNotIn(stray, [m["id"] for m in thread["messages"]])
        self.assertEqual(thread["status"], "waiting_orch")

    # -- outcomes -----------------------------------------------------------------------------------------

    def test_not_found_and_read_errors_are_distinct(self):
        q = self.ask()
        reply = self.answer(q)
        self.con.execute("INSERT INTO entries(id,orch_id,bound_at) VALUES('private',?,1)", (self.orch,))
        private = self.raw("out", "question", "private?", 1, entry_id="private", question_state="current")
        for kwargs in (dict(question_id=999999), dict(question_id=reply), dict(question_id=private),
                       dict(task_id="no-such-task"), dict(question_id=q, entry_id="other")):
            with self.subTest(kwargs=kwargs):
                payload, code = self.read(**kwargs)
                self.assertEqual((payload["status"], code, payload["threads"], payload["complete"]),
                                 ("not_found", 3, [], False))
        data = self.call_cli(["entry", "thread", "--question-id", "999999"], code=3)
        self.assertEqual(data["status"], "not_found")
        quiet = self.task()
        data = self.ok(task_id=quiet)
        self.assertEqual((data["threads"], data["complete"], data["thread_page"]["total"]), ([], True, 0))

        missing = Path(self.tmp.name) / "missing"
        missing.mkdir()
        data = self.call_cli(["entry", "thread", "--question-id", str(q)], code=5, home=str(missing))
        self.assertEqual((data["status"], data["threads"], data["complete"]), ("read_error", [], False))
        corrupt = Path(self.tmp.name) / "corrupt"
        corrupt.mkdir()
        (corrupt / "orchd.db").write_bytes(b"not a database" * 100)
        payload, code = entry_threads.read_questions(corrupt / "orchd.db", task_id="x")
        self.assertEqual((payload["status"], code), ("read_error", 5))

    def test_missing_history_is_incomplete_not_empty(self):
        task = self.task()
        q = self.ask("answered?", task)
        self.con.execute("UPDATE entry_messages SET question_state='answered' WHERE id=?", (q,))  # reply lost
        payload, code = self.read(question_id=q)
        self.assertEqual((payload["status"], code, payload["complete"]), ("incomplete", 4, False))
        thread, = payload["threads"]
        self.assertFalse(thread["complete"])
        self.assertEqual(thread["history_issues"], [dict(problem="answered_without_reply", question_id=q)])
        self.call_cli(["entry", "thread", "--task-id", task], code=4)
        self.con.execute("PRAGMA foreign_keys=OFF")
        later = self.ask("broken link?", task)
        self.con.execute("UPDATE entry_messages SET supersedes=? WHERE id=?", (987654, later))
        thread, = self.read(question_id=later)[0]["threads"]
        self.assertEqual(thread["history_issues"],
                         [dict(problem="missing_superseded_question", question_id=later, missing_id=987654)])

    def test_full_text_is_never_truncated(self):
        body = "中" * 300 + "🙂é" + "x" * 300
        q = self.ask(body)
        reply = self.answer(q, "答" * 250)
        thread, = self.ok(question_id=q)["threads"]
        self.assertEqual(thread["root_question"]["body"], body)
        self.assertEqual([m["text"] for m in thread["messages"]], ["答" * 250, body])
        raw = self.call_cli(["entry", "thread", "--question-id", str(q), "--json"])
        self.assertEqual(raw["threads"][0]["messages"][1]["text"], body)
        self.assertEqual(raw["threads"][0]["messages"][0]["id"], reply)

    def test_not_limited_by_answered_limit_and_cursor_pages(self):
        task = self.task()
        noise = self.task()
        for i in range(105):  # more answered questions than snapshot's default answered_limit
            nq = self.raw("out", "question", f"noise {i}", 1000 + i, task_id=noise, question_state="answered")
            self.raw("in", "reply", f"noise answer {i}", 1000 + i, reply_to=nq, task_id=noise)
        target = self.raw("out", "question", "target?", 10, task_id=task, question_state="current")
        replies = [self.raw("in", "reply", f"reply {i}", 20 + i, reply_to=target, task_id=task) for i in range(6)]
        expected = replies[::-1] + [target]
        seen, cursor, pages = [], None, 0
        while True:
            thread, = self.ok(question_id=target, limit=3, cursor=cursor)["threads"]
            seen += [m["id"] for m in thread["messages"]]
            pages += 1
            if not thread["has_more"]:
                self.assertIsNone(thread["next_cursor"])
                break
            cursor = thread["next_cursor"]
            self.assertIsInstance(cursor, str)
        self.assertEqual((seen, pages), (expected, 3))
        first = self.ok(task_id=task, per_question_limit=3)["threads"][0]
        self.assertEqual(([m["id"] for m in first["messages"]], first["has_more"]), (expected[:3], True))
        # A per-thread cursor from the task view continues with --question-id.
        page = self.call_cli(["entry", "thread", "--question-id", str(target), "--limit", "3",
                              "--cursor", first["next_cursor"]])
        self.assertEqual([m["id"] for m in page["threads"][0]["messages"]], expected[3:6])
        other = self.raw("out", "question", "other?", 5, task_id=task, question_state="queued")
        payload, code = self.read(question_id=other, cursor=first["next_cursor"])
        self.assertEqual((payload["status"], code), ("invalid_request", 2))
        payload, code = self.read(task_id=task, cursor=first["next_cursor"])  # message cursor is not a thread cursor
        self.assertEqual((payload["status"], code), ("invalid_request", 2))

    def test_task_thread_list_is_paginated(self):
        task = self.task()
        ids = [self.raw("out", "question", f"q{i}", 100 + i, task_id=task, question_state="queued") for i in range(5)]
        seen, cursor = [], None
        while True:
            data = self.ok(task_id=task, limit=2, per_question_limit=1, cursor=cursor)
            self.assertEqual(data["thread_page"]["total"], 5)
            seen += [t["question_id"] for t in data["threads"]]
            cursor = data["thread_page"]["next_cursor"]
            self.assertEqual(data["thread_page"]["has_more"], cursor is not None)
            if cursor is None:
                break
        self.assertEqual(seen, ids[::-1])
        payload, code = self.read(task_id=self.task(), cursor=self.ok(task_id=task, limit=2)["thread_page"]["next_cursor"])
        self.assertEqual((payload["status"], code), ("invalid_request", 2))

    def test_same_time_ties_break_by_id_descending(self):
        q = self.raw("out", "question", "tie?", 50, question_state="current")
        replies = [self.raw("in", "reply", f"tie {i}", 50, reply_to=q) for i in range(4)]
        first = self.ok(question_id=q, limit=2)["threads"][0]
        self.assertEqual([m["id"] for m in first["messages"]], replies[:1:-1])
        second = self.ok(question_id=q, limit=10, cursor=first["next_cursor"])["threads"][0]
        self.assertEqual([m["id"] for m in second["messages"]], [replies[1], replies[0], q])
        self.assertEqual(first, self.ok(question_id=q, limit=2)["threads"][0])  # deterministic

    def test_reads_leave_db_delivery_and_bindings_unchanged(self):
        task = self.task()
        q1 = self.ask("one?", task)
        self.ask("two?", task)  # queued and held: a write path would deliver it
        self.answer(q1)
        self.ask("three?", task)
        state = lambda: (self.con.execute("SELECT id,delivery,read_at,question_state,attempts FROM entry_messages "
                                          "ORDER BY id").fetchall(),
                         self.con.execute("SELECT * FROM entries ORDER BY id").fetchall())
        before_rows = [tuple(r) for r in state()[0]], [tuple(r) for r in state()[1]]
        before = self.db_bytes()
        sent = list(self.rt.sent)
        self.ok(question_id=q1)
        self.ok(task_id=task)
        self.call_cli(["entry", "thread", "--task-id", task, "--per-question-limit", "3", "--json"])
        self.read(question_id=999999)
        self.assertEqual(self.db_bytes(), before)
        self.assertEqual(([tuple(r) for r in state()[0]], [tuple(r) for r in state()[1]]), before_rows)
        self.assertEqual(self.rt.sent, sent)

    def test_issue_ref_default_limit_unchanged(self):
        ref = "jira:DEMO-44"
        for i in range(4):
            entry.send_to_nat(self.con, self.rt, self.orch, "x" * 250, issue_ref=ref)
        data = self.call_cli(["entry", "thread", "--issue-ref", ref, "--json"])
        self.assertEqual(len(data["threads"][ref]["messages"]), 3)
        self.assertEqual(len(data["threads"][ref]["messages"][0]["text"]), 200)


class OldSchemaTest(unittest.TestCase):
    def test_pre_revision_database_reads_without_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "old.db"
            con = sqlite3.connect(path)
            con.executescript(store.SCHEMA)
            con.execute("INSERT INTO entry_messages(entry_id,orch_id,direction,kind,body,body_bytes,body_sha256,"
                        "delivery,question_state,created_at) VALUES('desktop','old','out','question','legacy?',7,'h',"
                        "'delivered','current',1)")
            con.commit()
            con.close()
            before = path.read_bytes()
            payload, code = entry_threads.read_questions(path, question_id=1)
            self.assertEqual(code, 0, payload.get("error"))
            thread, = payload["threads"]
            self.assertEqual((thread["versions"], thread["version"], thread["issue_ref"]), ([1], 1, None))
            self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
