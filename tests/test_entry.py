"""Issue #37: the Desktop entry relays Nat's text to one fixed Claude Orch and back, verbatim and linked."""
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchd import core, entry, mcp_server, store
from orchd.runtime import Runtime
from tests.test_orchd import FakeRuntime

BODY = "請寄給 Ann：\r\n\t「quoted」 'single'\n```sh\necho $HOME\n```\né trailing  \nEND"


class EntryRuntime(FakeRuntime):
    def __init__(self):
        super().__init__()
        self.jobs = {"orchjob": {}, "job1": {}}
        self.user_messages = {}  # thread -> [{turn_id, item_id, text}]
        self.uds_fails = False

    def codex_user_messages(self, thread):
        return list(self.user_messages.get(thread, []))

    def send_uds(self, path, session_id, text):
        if self.uds_fails:
            raise ConnectionRefusedError("socket gone")
        super().send_uds(path, session_id, text)

    def nat_says(self, thread, text, turn="turn-1", item=None):
        rows = self.user_messages.setdefault(thread, [])
        rows.append({"turn_id": turn, "item_id": item or f"item-{len(rows) + 1}", "text": text})


class EntryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = store.connect(Path(self.tmp.name) / "t.db")
        self.rt = EntryRuntime()
        self.orch = core.start_orch(self.con, self.rt, "opus")["id"]
        entry.bind(self.con, self.rt, self.orch)
        self.rt.sent.clear()

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def relay(self, thread="desk-1", **kw):
        return entry.relay(self.con, self.rt, "desktop", thread, **kw)

    def task(self):
        t = core.dispatch(self.con, self.rt, orch_thread=self.orch, repo="demo", title="T", instructions="i",
                          done_when="d", model="sonnet", model_reason="r", task_type="outward")
        return t["id"]

    # -- Nat -> Orch -----------------------------------------------------------------------------------

    def test_nat_text_reaches_the_orch_verbatim_from_the_rollout(self):
        self.rt.nat_says("desk-1", BODY)
        result = self.relay()
        self.assertEqual(result["status"], "delivered")
        self.assertEqual(result["body_sha256"], hashlib.sha256(BODY.encode()).hexdigest())
        self.assertEqual(result["body_bytes"], len(BODY.encode()))
        self.assertNotIn("body", result)  # the entry reports status, never the text it could retype
        (path, session, wake), = self.rt.sent
        self.assertEqual(path, f"/tmp/orchd-o-{self.orch}/o.sock")
        self.assertNotIn(BODY, wake)  # metadata only on the wire; the body comes from the store
        got = entry.inbox(self.con, self.orch)
        self.assertEqual(got["messages"][0]["body"], BODY)
        self.assertEqual(got["messages"][0]["body_sha256"], result["body_sha256"])
        self.assertEqual(entry.inbox(self.con, self.orch)["messages"], [])  # read once

    def test_same_source_item_is_relayed_once(self):
        self.rt.nat_says("desk-1", "hi")
        first = self.relay()
        again = self.relay()
        self.assertEqual(again["status"], "duplicate")
        self.assertEqual(again["message_id"], first["message_id"])
        self.assertEqual(len(self.rt.sent), 1)

    def test_turn_without_persisted_source_is_not_replaced_by_an_older_message(self):
        self.rt.nat_says("desk-1", "old", turn="turn-1")
        self.relay(turn_id="turn-1")
        result = self.relay(turn_id="turn-2")
        self.assertEqual(result["status"], "source_not_ready")
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM entry_messages").fetchone()[0], 1)

    def test_orchd_own_queued_text_is_not_relayed_as_nat(self):
        sent = entry.send_to_nat(self.con, self.rt, self.orch, "status update")
        self.relay_thread_seen()
        wire = self.con.execute("SELECT wire_text FROM entry_messages WHERE id=?", (sent["message_id"],)).fetchone()[0]
        self.rt.nat_says("desk-1", wire)
        with self.assertRaisesRegex(ValueError, "orchd's own message"):
            self.relay()

    def relay_thread_seen(self, thread="desk-1"):
        entry.status(self.con, self.rt, "desktop", thread)

    # -- Q1: offline -----------------------------------------------------------------------------------

    def test_offline_orch_keeps_the_message_and_starts_nothing(self):
        self.rt.jobs = {}
        self.rt.orch_started = None
        self.rt.nat_says("desk-1", BODY)
        result = self.relay()
        self.assertEqual(result["status"], "not_delivered")
        self.assertIn("offline", result["note"])
        self.assertEqual(self.rt.sent, [])
        self.assertIsNone(self.rt.orch_started)
        self.assertEqual(store.get_orch(self.con, self.orch)["stopped_at"], None)
        # back online: the kept message goes out on the next call, still verbatim
        self.rt.jobs = {"orchjob": {}}
        status = entry.status(self.con, self.rt, "desktop", "desk-1")
        self.assertEqual(status["not_yet_delivered_to_orch"], [])
        self.assertEqual(len(self.rt.sent), 1)
        self.assertEqual(entry.inbox(self.con, self.orch)["messages"][0]["body"], BODY)

    def test_stopped_orch_is_offline(self):
        core.stop_orch(self.con, self.rt, self.orch)
        self.rt.nat_says("desk-1", "x")
        self.assertEqual(self.relay()["status"], "not_delivered")

    def test_socket_failure_is_kept_and_retried_in_order(self):
        self.rt.uds_fails = True
        self.rt.nat_says("desk-1", "one")
        self.assertEqual(self.relay()["status"], "failed")
        self.rt.nat_says("desk-1", "two")
        self.assertEqual(self.relay()["status"], "pending")  # never overtakes the earlier one
        self.rt.uds_fails = False
        self.rt.nat_says("desk-1", "three")
        self.assertEqual(self.relay()["status"], "delivered")
        self.assertEqual([m["body"] for m in entry.inbox(self.con, self.orch)["messages"]], ["one", "two", "three"])

    def test_receipt_failure_is_uncertain_and_not_resent(self):
        self.rt.nat_says("desk-1", "once")
        real = self.con.execute

        class Con:
            def __getattr__(_, name):
                return getattr(self.con, name)

            def execute(_, sql, *args):
                if "delivery='delivered'" in sql:
                    raise OSError("disk full")
                return real(sql, *args)

        result = entry.relay(Con(), self.rt, "desktop", "desk-1")
        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(len(self.rt.sent), 1)
        entry.status(self.con, self.rt, "desktop", "desk-1")
        self.assertEqual(len(self.rt.sent), 1)  # never sent twice
        self.assertEqual(entry.snapshot(self.con, self.rt)["counts"][0]["state"], "uncertain")

    # -- Q2: one question at a time --------------------------------------------------------------------

    def test_one_question_at_a_time_and_replies_are_linked(self):
        self.relay_thread_seen()
        t1, t2 = self.task(), self.task()
        core.ask(self.con, self.rt, t1, "Send this email?\n---\nDear Ann,\r\nthanks\n---")
        q1 = entry.ask_nat(self.con, self.rt, self.orch, "OK to send?", t1, quote_worker_question=True)
        q2 = entry.ask_nat(self.con, self.rt, self.orch, "Which branch?", t2)
        self.assertEqual((q1["state"], q2["state"]), ("current", "queued"))
        woken = [w for w in self.rt.woken if w[1] == "desk-1"]
        self.assertEqual(len(woken), 1)  # only q1 reached Nat
        self.assertIn("Dear Ann,\r\nthanks", woken[0][2])  # the worker preview, byte for byte
        self.assertIn(f"[orchd question {q1['question_id']}]", woken[0][2])

        self.rt.nat_says("desk-1", "yes, send it")
        reply = self.relay(reply_to=q1["question_id"])
        self.assertEqual((reply["kind"], reply["task_id"], reply["reply_to"]), ("reply", t1, q1["question_id"]))
        woken = [w for w in self.rt.woken if w[1] == "desk-1"]
        self.assertEqual(len(woken), 2)
        self.assertIn(f"[orchd question {q2['question_id']}]", woken[1][2])  # next one promoted

        got = entry.inbox(self.con, self.orch)
        (msg,) = got["messages"]
        self.assertEqual((msg["task_id"], msg["reply_to"]), (t1, q1["question_id"]))
        self.assertEqual(got["current_question"], {"id": q2["question_id"], "task_id": t2})

    def test_stale_duplicate_or_unasked_reply_is_refused(self):
        self.relay_thread_seen()
        t1 = self.task()
        q1 = entry.ask_nat(self.con, self.rt, self.orch, "A?", t1)
        q2 = entry.ask_nat(self.con, self.rt, self.orch, "B?", t1)
        self.rt.nat_says("desk-1", "answer B early")
        with self.assertRaisesRegex(ValueError, "not been asked"):
            self.relay(reply_to=q2["question_id"])
        self.rt.nat_says("desk-1", "answer A", item="a")
        self.relay(reply_to=q1["question_id"])
        self.rt.nat_says("desk-1", "answer A again", item="a2")
        with self.assertRaisesRegex(ValueError, "already answered"):
            self.relay(reply_to=q1["question_id"])
        with self.assertRaisesRegex(ValueError, "not a question"):
            self.relay(reply_to=99999)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM entry_messages WHERE kind='reply'").fetchone()[0], 1)

    def test_message_without_reply_to_is_not_taken_as_the_answer(self):
        self.relay_thread_seen()
        q = entry.ask_nat(self.con, self.rt, self.orch, "Approve?")
        self.rt.nat_says("desk-1", "ok")
        result = self.relay()
        self.assertEqual(result["kind"], "message")
        self.assertIn("still open", result["note"])
        self.assertEqual(entry.inbox(self.con, self.orch)["current_question"]["id"], q["question_id"])

    def test_answer_forwards_the_stored_reply_to_the_right_task_only(self):
        self.relay_thread_seen()
        t1, t2 = self.task(), self.task()
        core.ask(self.con, self.rt, t1, "send?")
        q = entry.ask_nat(self.con, self.rt, self.orch, "send?", t1)
        self.rt.nat_says("desk-1", BODY)
        reply = self.relay(reply_to=q["question_id"])
        with patch.dict(os.environ, {"ORCHD_ORCH_ID": self.orch}):
            bad = self.call_orch("answer", {"task_id": t2, "entry_reply_id": reply["message_id"]})
            self.assertTrue(bad["isError"])
            self.rt.sent.clear()
            ok = self.call_orch("answer", {"task_id": t1, "entry_reply_id": reply["message_id"]})
        self.assertNotIn("isError", ok)
        self.assertIn(BODY, self.rt.sent[-1][2])
        self.rt.nat_says("desk-1", "unlinked")
        plain = self.relay()
        with patch.dict(os.environ, {"ORCHD_ORCH_ID": self.orch}):
            refused = self.call_orch("answer", {"task_id": t1, "entry_reply_id": plain["message_id"]})
        self.assertTrue(refused["isError"])
        self.assertIn("not a reply", refused["content"][0]["text"])

    def call_orch(self, name, args):
        return mcp_server.handle({"id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}},
                                 self.con, self.rt)["result"]

    # -- Q3: reopen ------------------------------------------------------------------------------------

    def test_reopened_entry_keeps_the_binding_and_open_question(self):
        self.relay_thread_seen("desk-1")
        q = entry.ask_nat(self.con, self.rt, self.orch, "Still there?")
        before = self.con.execute("SELECT COUNT(*) FROM orchs").fetchone()[0]
        status = entry.status(self.con, self.rt, "desktop", "desk-2")
        self.assertTrue(status["resumed"])
        self.assertEqual(status["orch_id"], self.orch)
        self.assertEqual(status["current_question"]["message_id"], q["question_id"])
        self.assertEqual(status["current_question"]["body"], "Still there?")
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM orchs").fetchone()[0], before)
        entry.send_to_nat(self.con, self.rt, self.orch, "after reopen")
        self.assertEqual(self.rt.woken[-1][1], "desk-2")  # only the current thread
        # a message that failed to reach the old thread goes to the reopened one
        self.rt.wake_fails = True
        entry.send_to_nat(self.con, self.rt, self.orch, "missed")
        self.rt.wake_fails = False
        entry.status(self.con, self.rt, "desktop", "desk-3")
        self.assertEqual(self.rt.woken[-1][1], "desk-3")
        self.assertEqual(self.rt.woken[-1][2].split("\n\n", 1)[1], "missed")

    def test_reopen_with_orch_offline_reports_it_and_starts_nothing(self):
        self.rt.jobs = {}
        self.rt.orch_started = None
        status = entry.status(self.con, self.rt, "desktop", "desk-9")
        self.assertEqual(status["orch_health"]["state"], "dead")
        self.assertIsNone(self.rt.orch_started)

    def test_messages_before_any_entry_thread_wait_and_are_handed_over_by_status(self):
        sent = entry.send_to_nat(self.con, self.rt, self.orch, BODY)
        self.assertEqual(sent["delivery"], "pending")
        self.assertEqual(self.rt.woken, [])
        entry.status(self.con, self.rt, "desktop", "desk-1")  # the first entry call learns the thread
        (woken,) = self.rt.woken
        self.assertEqual(woken[1], "desk-1")
        self.assertEqual(woken[2].split("\n\n", 1)[1], BODY)
        # with the Desktop queue down, status hands the text over in its own result instead
        self.rt.wake_fails = True
        entry.send_to_nat(self.con, self.rt, self.orch, "second")
        status = entry.status(self.con, self.rt, "desktop", "desk-1")
        self.assertEqual([m["body"] for m in status["from_orch"]], ["second"])
        self.assertEqual(entry.status(self.con, self.rt, "desktop", "desk-1")["from_orch"], [])

    def test_queue_failure_is_retried_and_reaches_only_the_entry_thread(self):
        self.relay_thread_seen()
        self.rt.wake_fails = True
        self.assertEqual(entry.send_to_nat(self.con, self.rt, self.orch, "x")["delivery"], "failed")
        self.rt.wake_fails = False
        entry.send_to_nat(self.con, self.rt, self.orch, "y")
        self.assertEqual([w[1] for w in self.rt.woken], ["desk-1", "desk-1"])
        self.assertEqual([w[2].split("\n\n", 1)[1] for w in self.rt.woken], ["x", "y"])

    # -- binding and permissions -----------------------------------------------------------------------

    def test_bind_refuses_codex_stopped_dead_and_silent_rebind(self):
        store.register_orch(self.con, "codex-thread", "codex")
        with self.assertRaisesRegex(ValueError, "Claude Orch"):
            entry.bind(self.con, self.rt, "codex-thread")
        other = core.start_orch(self.con, self.rt, "opus")["id"]
        with self.assertRaisesRegex(ValueError, "needs --force"):
            entry.bind(self.con, self.rt, other)
        self.rt.jobs = {}
        with self.assertRaisesRegex(ValueError, "offline"):
            entry.bind(self.con, self.rt, other, force=True)

    def test_orch_side_tools_refuse_any_other_caller(self):
        store.register_orch(self.con, "codex-thread", "codex")
        for orch in ("codex-thread", None):
            with self.assertRaisesRegex(ValueError, "no Desktop entry"):
                entry.send_to_nat(self.con, self.rt, orch, "x")
            with self.assertRaisesRegex(ValueError, "no Desktop entry"):
                entry.inbox(self.con, orch)

    def entry_call(self, name, args=None, thread="desk-1"):
        return mcp_server.handle({"id": 1, "method": "tools/call", "params": {
            "name": name, "arguments": args or {}, "_meta": {"threadId": thread}}},
            self.con, self.rt, role="entry")["result"]

    def test_entry_role_lists_and_runs_only_entry_tools_and_never_registers(self):
        listed = mcp_server.handle({"id": 1, "method": "tools/list"}, self.con, self.rt, role="entry")
        self.assertEqual({t["name"] for t in listed["result"]["tools"]}, {"relay", "status"})
        init = mcp_server.handle({"id": 1, "method": "initialize", "params": {}}, self.con, self.rt, role="entry")
        self.assertIn("never retype", init["result"]["instructions"])
        before = self.con.execute("SELECT COUNT(*) FROM orchs").fetchone()[0]
        for name in ("dispatch", "answer", "close", "retry", "followup", "inbox", "send_to_nat", "ask_nat",
                     "entry_inbox", "list_open", "view_worker"):
            result = self.entry_call(name, {"task_id": "x"})
            self.assertTrue(result["isError"], name)
            self.assertIn("not available to the Desktop entry", result["content"][0]["text"])
        with patch.dict(os.environ, {"ORCHD_ORCH_ID": self.orch}):  # an Orch id in env changes nothing
            self.assertTrue(self.entry_call("dispatch")["isError"])
        self.assertTrue(self.entry_call("relay", {"text": "typed by the model"})["isError"])
        self.rt.nat_says("desk-1", "hello")
        ok = self.entry_call("relay")
        self.assertNotIn("isError", ok)
        self.assertEqual(json.loads(ok["content"][0]["text"])["status"], "delivered")
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM orchs").fetchone()[0], before)
        self.assertEqual({t["name"] for t in mcp_server.TOOLS} & {"relay", "status"}, set())


class DeskTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.con = store.connect(root / "t.db")
        self.rt = EntryRuntime()
        self.home = root / "desk"
        codex = root / "codex"
        codex.mkdir()
        self.codex_cfg = codex / "config.toml"
        self.env = patch.dict(os.environ, {"ORCHD_DESK_HOME": str(self.home), "CODEX_HOME": str(codex)})
        self.env.start()
        self.starts = 0

    def tearDown(self):
        self.env.stop()
        self.con.close()
        self.tmp.cleanup()

    def start(self):
        self.starts += 1
        return core.start_orch(self.con, self.rt, "opus")

    def desk(self, new=False):
        return entry.desk(self.con, self.rt, self.start, "/opt/orchd/bin/orchd", new)

    def test_first_run_prepares_the_folder_starts_and_binds_one_orch_then_reuses_it(self):
        first = self.desk()
        self.assertTrue(first["started_new_orch"])
        self.assertEqual(first["files"], {".codex/config.toml": "created", "AGENTS.md": "created"})
        cfg = (self.home / ".codex" / "config.toml").read_text()
        for line in ('model = "gpt-6.1-sol"', 'model_reasoning_effort = "low"', 'default_permissions = ":read-only"',
                     'approval_policy = "never"', '"mcp", "--role", "entry"'):
            self.assertIn(line, cfg)
        self.assertIn("逐字唸出", (self.home / "AGENTS.md").read_text())
        self.assertEqual(entry.get_entry(self.con, "desktop")["orch_id"], first["orch_id"])
        second = self.desk()
        self.assertEqual((second["orch_id"], second["started_new_orch"], self.starts), (first["orch_id"], False, 1))
        self.assertEqual(set(second["files"].values()), {"unchanged"})

    def test_offline_orch_is_not_replaced_without_new(self):
        orch = self.desk()["orch_id"]
        entry.ask_nat(self.con, self.rt, orch, "open?")
        self.rt.jobs = {}
        with self.assertRaisesRegex(ValueError, "offline.*1 open question.*--new"):
            self.desk()
        self.assertEqual(self.starts, 1)
        self.rt.jobs = {"orchjob": {}}
        store.stop_orch(self.con, orch)
        replaced = self.desk(new=True)
        self.assertTrue(replaced["started_new_orch"])
        self.assertNotEqual(replaced["orch_id"], orch)
        self.assertEqual(entry.get_entry(self.con, "desktop")["orch_id"], replaced["orch_id"])

    def test_hand_edited_files_are_kept(self):
        (self.home / ".codex").mkdir(parents=True)
        (self.home / ".codex" / "config.toml").write_text("model = \"mine\"\n")
        result = self.desk()
        self.assertTrue(result["files"][".codex/config.toml"].startswith("kept"))
        self.assertEqual((self.home / ".codex" / "config.toml").read_text(), "model = \"mine\"\n")

    def test_reports_codex_trust_without_writing_it(self):
        self.assertFalse(self.desk()["codex_trusted"])
        self.codex_cfg.write_text(f'[projects."{self.home.resolve()}"]\ntrust_level = "trusted"\n')
        result = self.desk()
        self.assertTrue(result["codex_trusted"])
        self.assertIn("start talking", result["next"])
        self.assertEqual(self.codex_cfg.read_text(), f'[projects."{self.home.resolve()}"]\ntrust_level = "trusted"\n')


class RolloutSourceTest(unittest.TestCase):
    def test_reads_user_messages_exactly_from_the_rollout(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "2026/10/03/rollout-2026-10-03T00-00-00-thread-X.jsonl"
            path.parent.mkdir(parents=True)
            lines = [
                {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "t1"}},
                {"type": "event_msg", "payload": {"type": "item_completed", "turn_id": "t1", "item": {
                    "type": "UserMessage", "id": "i1", "content": [{"type": "text", "text": BODY[:20]},
                                                                   {"type": "text", "text": BODY[20:]}]}}},
                {"type": "event_msg", "payload": {"type": "item_completed", "turn_id": "t1", "item": {
                    "type": "AgentMessage", "id": "i2", "content": [{"type": "text", "text": "retyped"}]}}},
                {"type": "response_item", "payload": {"type": "message", "role": "user",
                                                      "content": [{"type": "input_text", "text": "dup"}]}},
            ]
            path.write_text("".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines) + "not json\n")
            with patch.dict(os.environ, {"ORCHD_CODEX_SESSIONS": root}):
                got = Runtime().codex_user_messages("thread-X")
                self.assertEqual(got, [{"turn_id": "t1", "item_id": "i1", "text": BODY}])
                self.assertEqual(Runtime().codex_user_messages("thread-missing"), [])


if __name__ == "__main__":
    unittest.main()
