"""Issue #37: the Desktop entry relays Nat's text to one fixed Claude Orch and back, verbatim and linked."""
import hashlib
import json
import os
import sqlite3
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
        with self.assertRaisesRegex(ValueError, "needs force"):
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


VOICE_ROUND_2 = """<realtime_delegation>
  <input>應該是O2CH吧</input>
  <transcript_delta>assistant: 好的。
user: 請O區回我一句測試訊息
assistant: 我問一下。 O 區回覆:「收到,這裡是 O 區,測試訊息回覆完成。」
user: 應該是O2CH吧</transcript_delta>
</realtime_delegation>"""
VOICE_FLUSH = """<realtime_delegation>
  <source>transcript_tail_flush</source>
  <input>The user just ended their realtime session. Here is the remaining handoff/transcript tail. You probably do not have to do anything; acknowledge the handoff unless the transcript itself asks for something.</input>
  <transcript_delta>assistant: 了解,是 Orch。
user: ORCH,O-Orchestrator 的意思啊
user: 收到</transcript_delta>
</realtime_delegation>"""


class VoiceTest(unittest.TestCase):
    """Issue #64: Desktop voice mode wraps Nat's words in <realtime_delegation>."""
    setUp, tearDown = EntryTest.setUp, EntryTest.tearDown
    relay, relay_thread_seen = EntryTest.relay, EntryTest.relay_thread_seen

    def test_parser(self):
        self.assertIsNone(entry.voice_input("plain typed text"))
        self.assertEqual(entry.voice_input(VOICE_FLUSH), {"handoff": True})
        self.assertEqual(entry.voice_input(VOICE_ROUND_2)["text"],
                         "[語音輸入，可能有辨識錯字]\n應該是O2CH吧")  # earlier rounds dropped, same words not repeated
        mixed = VOICE_ROUND_2.replace("<input>應該是O2CH吧</input>", "<input>Nat 說應該是 Orch</input>")
        self.assertEqual(entry.voice_input(mixed)["text"],
                         "[語音輸入，可能有辨識錯字]\nNat 說應該是 Orch\n（語音逐字稿：應該是O2CH吧）")
        wrapped = VOICE_ROUND_2.replace("user: 應該是O2CH吧", "user: 第一行\n第二行")
        self.assertIn("第一行\n第二行", entry.voice_input(wrapped)["text"])

    def test_voice_message_forwards_this_round_and_keeps_the_raw_text(self):
        self.rt.nat_says("desk-1", VOICE_ROUND_2)
        result = self.relay()
        self.assertEqual(result["status"], "delivered")
        self.assertIn("voice message", self.rt.sent[-1][2])
        (msg,) = entry.inbox(self.con, self.orch)["messages"]
        self.assertEqual(msg["body"], "[語音輸入，可能有辨識錯字]\n應該是O2CH吧")
        self.assertNotIn("請O區回我一句測試訊息", msg["body"])
        raw = self.con.execute("SELECT source_raw FROM entry_messages WHERE id=?", (msg["message_id"],)).fetchone()[0]
        self.assertEqual(raw, VOICE_ROUND_2)

    def test_end_of_session_handoff_is_kept_but_never_reaches_the_orch(self):
        self.rt.nat_says("desk-1", VOICE_FLUSH)
        result = self.relay()
        self.assertEqual(result["status"], "skipped_handoff")
        self.assertEqual(self.rt.sent, [])
        self.assertEqual(entry.inbox(self.con, self.orch)["messages"], [])
        self.assertEqual(entry.status(self.con, self.rt, "desktop", "desk-1")["not_yet_delivered_to_orch"], [])
        self.assertEqual(self.relay()["status"], "duplicate")
        self.assertEqual(self.rt.sent, [])

    def voice(self, said, turn, item):
        self.rt.nat_says("desk-1", f"<realtime_delegation>\n  <input>{said}</input>\n"
                                   f"  <transcript_delta>user: {said}</transcript_delta>\n</realtime_delegation>",
                         turn=turn, item=item)
        return self.relay(turn_id=turn)

    def test_growing_sentence_in_one_turn_forwards_only_what_is_new(self):
        # Nat's machine, 2026-10-04: entry messages 12 and 14 came from one turn; 14 repeated 12's request.
        self.assertEqual(self.voice("OK,記得回我一個測試訊息", "t1", "a")["status"], "delivered")
        second = self.voice("OK,記得回我一個測試訊息有聽到嗎OK,確認回我一個測試訊息嗯", "t1", "b")
        self.assertEqual(second["status"], "delivered")
        bodies = [m["body"] for m in entry.inbox(self.con, self.orch)["messages"]]
        self.assertEqual(bodies[1], "[語音輸入，可能有辨識錯字]（接續上一則）\n有聽到嗎OK,確認回我一個測試訊息嗯")
        same = self.voice("OK,記得回我一個測試訊息有聽到嗎OK,確認回我一個測試訊息嗯", "t1", "c")
        self.assertEqual((same["status"], same["kind"]), ("duplicate", "voice_repeat"))
        self.assertEqual(len(self.rt.sent), 2)  # the re-send never woke the Orch
        self.assertEqual(entry.inbox(self.con, self.orch)["messages"], [])

    def test_utterance_split_across_turns_is_not_repeated(self):
        # Nat's machine, 2026-10-04 entry messages 18 and 20: two turns, the second transcript repeats the first.
        self.voice("請獲取 回我一個測試訊息", "t1", "a")
        self.rt.nat_says("desk-1", "<realtime_delegation>\n  <input>Orch</input>\n  <transcript_delta>"
                                   "assistant:  好的,我來處理一下。\nuser: 請獲取 回我一個測試訊息\n"
                                   "user: 跟之前不太一樣的Orch\nuser: Orch</transcript_delta>\n</realtime_delegation>",
                         turn="t2", item="b")
        self.relay(turn_id="t2")
        bodies = [m["body"] for m in entry.inbox(self.con, self.orch)["messages"]]
        self.assertEqual(bodies[1], "[語音輸入，可能有辨識錯字]\nOrch\n（語音逐字稿：跟之前不太一樣的Orch\nOrch）")
        self.assertNotIn("請獲取", bodies[1])

    def test_same_words_in_a_later_turn_are_not_cut(self):
        self.voice("寄信", "t1", "a")
        self.voice("寄信給 Ann", "t2", "b")
        bodies = [m["body"] for m in entry.inbox(self.con, self.orch)["messages"]]
        self.assertEqual(bodies, ["[語音輸入，可能有辨識錯字]\n寄信", "[語音輸入，可能有辨識錯字]\n寄信給 Ann"])

    def test_voice_answer_still_links_to_the_question(self):
        self.relay_thread_seen()
        q = entry.ask_nat(self.con, self.rt, self.orch, "寄嗎？")
        self.rt.nat_says("desk-1", VOICE_ROUND_2.replace("應該是O2CH吧", "寄吧"))
        reply = self.relay(reply_to=q["question_id"])
        self.assertEqual((reply["kind"], reply["reply_to"]), ("reply", q["question_id"]))
        self.assertEqual(entry.inbox(self.con, self.orch)["messages"][0]["body"], "[語音輸入，可能有辨識錯字]\n寄吧")


class InterfaceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.con = store.connect(root / "t.db")
        self.rt = EntryRuntime()
        self.home = root / "interface"
        (self.home / ".codex").mkdir(parents=True)
        (self.home / ".codex" / "config.toml").write_text("# from orchd init\n")
        codex = root / "codex"
        codex.mkdir()
        self.codex_cfg = codex / "config.toml"
        self.env = patch.dict(os.environ, {"ORCHD_INTERFACE_HOME": str(self.home), "CODEX_HOME": str(codex)})
        self.env.start()
        self.starts = 0

    def tearDown(self):
        self.env.stop()
        self.con.close()
        self.tmp.cleanup()

    def start(self):
        self.starts += 1
        return core.start_orch(self.con, self.rt, "opus")

    def interface(self, new=False):
        return entry.binding(self.con, self.rt, self.start, new)

    def test_first_run_starts_and_binds_one_orch_then_reuses_it(self):
        first = self.interface()
        self.assertTrue(first["started_new_orch"])
        self.assertEqual(entry.get_entry(self.con, "desktop")["orch_id"], first["orch_id"])
        second = self.interface()
        self.assertEqual((second["orch_id"], second["started_new_orch"], self.starts), (first["orch_id"], False, 1))

    def test_refuses_before_init(self):
        (self.home / ".codex" / "config.toml").unlink()
        with self.assertRaisesRegex(ValueError, "orchd init"):
            self.interface()
        self.assertEqual(self.starts, 0)

    def test_offline_orch_is_not_replaced_without_new(self):
        orch = self.interface()["orch_id"]
        entry.ask_nat(self.con, self.rt, orch, "open?")
        self.rt.jobs = {}
        with self.assertRaisesRegex(ValueError, "offline.*1 open question.*orchd binding --new"):
            self.interface()
        self.assertEqual(self.starts, 1)
        self.rt.jobs = {"orchjob": {}}
        store.stop_orch(self.con, orch)
        replaced = self.interface(new=True)
        self.assertTrue(replaced["started_new_orch"])
        self.assertNotEqual(replaced["orch_id"], orch)
        self.assertEqual(entry.get_entry(self.con, "desktop")["orch_id"], replaced["orch_id"])

    def test_reports_codex_trust_without_writing_it(self):
        self.assertFalse(self.interface()["codex_trusted"])
        self.codex_cfg.write_text(f'[projects."{self.home.resolve()}"]\ntrust_level = "trusted"\n')
        result = self.interface()
        self.assertTrue(result["codex_trusted"])
        self.assertIn("start talking", result["next"])


class EarlyDraftSchemaTest(unittest.TestCase):
    def test_db_with_the_early_draft_entry_table_opens_and_works(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.db"
            old = sqlite3.connect(path)
            old.executescript("""
                CREATE TABLE entries(id TEXT PRIMARY KEY, orch_id TEXT NOT NULL, thread_id TEXT, bound_at REAL NOT NULL,
                                     thread_seen_at REAL);
                CREATE TABLE entry_messages(id INTEGER PRIMARY KEY AUTOINCREMENT, entry_id TEXT NOT NULL, orch_id TEXT NOT NULL,
                    direction TEXT NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL, task_id TEXT, source_message_id INTEGER,
                    reply_to INTEGER, question_state TEXT, delivery TEXT NOT NULL, delivery_error TEXT, recipient TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, delivered_at REAL, read_at REAL);""")
            old.close()
            con = store.connect(path)
            store.connect(path).close()  # a second open is a no-op
            rt = EntryRuntime()
            orch = core.start_orch(con, rt, "opus")["id"]
            entry.bind(con, rt, orch)
            rt.nat_says("desk-1", BODY)
            self.assertEqual(entry.relay(con, rt, "desktop", "desk-1")["status"], "delivered")
            self.assertEqual(entry.relay(con, rt, "desktop", "desk-1")["status"], "duplicate")
            self.assertEqual(entry.inbox(con, orch)["messages"][0]["body"], BODY)
            con.close()


class BindingCliTest(unittest.TestCase):
    """Issue #67: `orchd binding` replaces interface / entry-bind / entry-status."""

    def test_modes(self):
        import contextlib, io
        from orchd import cli
        with tempfile.TemporaryDirectory() as tmp:
            rt = EntryRuntime()
            con = store.connect(Path(tmp) / "orchd.db")
            orch = core.start_orch(con, rt, "opus")["id"]
            def run(*argv):
                out, err = io.StringIO(), io.StringIO()
                with patch.dict(os.environ, {"ORCHD_HOME": tmp}), patch.object(cli, "Runtime", lambda: rt), \
                        contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    return cli.main(list(argv)), out.getvalue(), err.getvalue()
            code, _, err = run("binding", "--status")
            self.assertEqual(code, 1)
            self.assertIn("orchd binding", err)
            code, out, _ = run("binding", "--to", orch)
            self.assertEqual((code, json.loads(out)["orch_id"]), (0, orch))
            code, out, _ = run("binding", "--status")
            self.assertEqual((code, json.loads(out)["orch_id"]), (0, orch))
            con2 = store.connect(Path(tmp) / "orchd.db")
            self.assertEqual(entry.get_entry(con2, "desktop")["orch_id"], orch)
            for old in ("interface", "entry-bind", "entry-status"):
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    cli.main([old])
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                cli.main(["binding", "--new", "--status"])


class RolloutSourceTest(unittest.TestCase):
    def test_reads_user_messages_exactly_from_the_rollout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = str(Path(tmp) / "sessions")
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
            archived = Path(root).parent / "archived_sessions"
            archived.mkdir()
            path.rename(archived / path.name)
            with patch.dict(os.environ, {"ORCHD_CODEX_SESSIONS": root}):
                self.assertEqual(Runtime().codex_user_messages("thread-X")[0]["text"], BODY)


if __name__ == "__main__":
    unittest.main()
