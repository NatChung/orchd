"""Two Desktop chats through Entry MCP, SQLite, and the Orch/worker routing layer."""
import io
import json
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from orchd import bootstrap, core, mcp_server, store
from orchd.runtime import Runtime
from tests.test_orchd import FakeRuntime


class DesktopRuntime(FakeRuntime):
    codex_user_messages = Runtime.codex_user_messages

    def start_orch(self, orch_id, model, orch_home, resume=None):
        if resume:
            return super().start_orch(orch_id, model, orch_home, resume)
        job = f"job-{orch_id}"
        self.jobs[job] = {"pid": 4242, "status": "idle"}
        return self.orch_socket_path(orch_id), job, f"session-{orch_id}"


class DesktopPerThreadTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.interface = self.root / "orch" / "interface"
        self.config = self.interface / ".codex" / "config.toml"
        self.config.parent.mkdir(parents=True)
        self.config.touch()
        self.sessions = self.root / "codex" / "sessions"
        self.sessions.mkdir(parents=True)
        self.env = {
            "ORCHD_CODEX_SESSIONS": str(self.sessions),
            "ORCHD_CONFIG_DIR": str(self.root / "config"),
            "ORCHD_CONFIG_FILE": str(self.root / "config" / "init.toml"),
            "ORCHD_ROOT": str(self.root / "orch"),
            "ORCHD_INTERFACE_HOME": str(self.interface),
            "ORCHD_ORCH_HOME": str(self.root / "orch" / "home"),
            "ORCHD_HOME": str(self.root / "state"),
            "CODEX_HOME": str(self.sessions.parent),
        }
        env = patch.dict(os.environ, self.env)
        env.start()
        self.addCleanup(env.stop)
        self.db = self.root / "test.db"
        self.con = store.connect(self.db)
        self.addCleanup(self.con.close)
        self.rt = DesktopRuntime()

    def chat(self, *, cwd=None, originator="Codex Desktop"):
        thread = str(uuid4())
        header = {"type": "session_meta", "payload": {
            "id": thread, "originator": originator, "cwd": str(cwd or self.interface)}}
        self.rollout(thread).write_text(json.dumps(header) + "\n", encoding="utf-8")
        return thread

    def rollout(self, thread):
        return self.sessions / f"rollout-2026-11-26T00-00-00-{thread}.jsonl"

    def user_turn(self, thread, body):
        turn = str(uuid4())
        row = {"type": "event_msg", "payload": {"type": "item_completed", "turn_id": turn, "item": {
            "type": "UserMessage", "id": str(uuid4()), "content": [{"type": "text", "text": body}]}}}
        with self.rollout(thread).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row) + "\n")
        return turn

    def request(self, thread, turn, name, **args):
        return {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": name, "arguments": args, "_meta": {
                "threadId": thread, "x-codex-turn-metadata": {"turn_id": turn}}}}

    def desktop(self, thread, turn, name, **args):
        # Use a fresh stdio session and a fresh DB connection, as on Desktop reopen.
        stdin = io.StringIO(json.dumps(self.request(thread, turn, name, **args)) + "\n")
        stdout = io.StringIO()
        with closing(store.connect(self.db)) as con:
            mcp_server.serve(stdin, stdout, con, self.rt, role="entry", per_thread=True)
        return json.loads(stdout.getvalue())["result"]

    def data(self, result):
        self.assertFalse(result.get("isError"), result)
        return json.loads(result["content"][0]["text"])

    def orch(self, owner, name, **args):
        request = {"id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}}
        with patch.dict(os.environ, {"ORCHD_ORCH_ID": owner}):
            return mcp_server.handle(request, self.con, self.rt)["result"]

    def task(self, owner, label):
        return core.dispatch(self.con, self.rt, orch_thread=owner, repo=f"demo-{label}",
                             title=f"Task {label}", instructions="isolated", done_when="answered",
                             model="sonnet", model_reason="fixture", task_type="outward")["id"]

    def test_new_chats_reopen_and_dispatch_independent_work_and_answers(self):
        chat_a, chat_b = self.chat(), self.chat()
        turn_a, turn_b = self.user_turn(chat_a, "交辦甲：\n第一組"), self.user_turn(chat_b, "交辦乙：\n第二組")
        first_a = self.data(self.desktop(chat_a, turn_a, "status"))
        first_b = self.data(self.desktop(chat_b, turn_b, "status"))
        a, b = first_a["orch_id"], first_b["orch_id"]
        self.assertNotEqual(a, b)
        self.assertTrue(first_a["started_new_orch"])
        self.assertTrue(first_b["started_new_orch"])
        for chat, turn, owner, body in ((chat_a, turn_a, a, "交辦甲：\n第一組"),
                                        (chat_b, turn_b, b, "交辦乙：\n第二組")):
            delivered = self.data(self.desktop(chat, turn, "relay"))
            self.assertEqual(delivered["status"], "delivered")
            inbox = self.data(self.orch(owner, "entry_inbox"))
            self.assertEqual([item["body"] for item in inbox["messages"]], [body])

        task_a, task_b = self.task(a, "A"), self.task(b, "B")
        core.ask(self.con, self.rt, task_a, "甲要選哪一個？")
        core.ask(self.con, self.rt, task_b, "乙要選哪一個？")
        question_a = self.data(self.orch(a, "ask_nat", task_id=task_a, text="甲要選哪一個？"))["question_id"]
        question_b = self.data(self.orch(b, "ask_nat", task_id=task_b, text="乙要選哪一個？"))["question_id"]
        self.assertEqual([chat for _, chat, _ in self.rt.woken], [chat_a, chat_b])
        for (_, _, wire), question, task, body in (
                (self.rt.woken[0], question_a, task_a, "甲要選哪一個？"),
                (self.rt.woken[1], question_b, task_b, "乙要選哪一個？")):
            self.assertTrue(wire.startswith(f"[orchd question {question}]"))
            self.assertIn(f"task {task}.", wire)
            self.assertEqual(wire.split("\n\n", 1)[1], body)

        answer_a, answer_b = self.user_turn(chat_a, "甲選 A"), self.user_turn(chat_b, "乙選 B")
        for chat, turn, owner, question in ((chat_a, answer_a, a, question_a), (chat_b, answer_b, b, question_b)):
            reopened = self.data(self.desktop(chat, turn, "status"))
            self.assertEqual(reopened["orch_id"], owner)
            self.assertFalse(reopened["started_new_orch"])
            self.assertEqual(reopened["current_question"]["message_id"], question)

        refused = self.desktop(chat_a, answer_a, "relay", reply_to=question_b)
        self.assertTrue(refused.get("isError"), refused)
        received_a = self.data(self.desktop(chat_a, answer_a, "relay", reply_to=question_a))
        still_b = self.data(self.desktop(chat_b, answer_b, "status"))
        self.assertEqual(still_b["current_question"]["message_id"], question_b)
        received_b = self.data(self.desktop(chat_b, answer_b, "relay", reply_to=question_b))
        cross_worker = self.orch(a, "answer", task_id=task_a, entry_reply_id=received_b["message_id"])
        self.assertTrue(cross_worker.get("isError"), cross_worker)

        self.rt.sent.clear()
        self.data(self.orch(a, "answer", task_id=task_a, entry_reply_id=received_a["message_id"]))
        self.data(self.orch(b, "answer", task_id=task_b, entry_reply_id=received_b["message_id"]))
        self.assertEqual([path for path, _, _ in self.rt.sent],
                         [self.rt.socket_path(task_a), self.rt.socket_path(task_b)])
        self.assertIn("甲選 A", self.rt.sent[0][2])
        self.assertNotIn("乙選 B", self.rt.sent[0][2])
        self.assertIn("乙選 B", self.rt.sent[1][2])
        self.assertNotIn("甲選 A", self.rt.sent[1][2])
        self.assertEqual([tuple(row) for row in self.con.execute(
            "SELECT id, orch_id, thread_id FROM entries ORDER BY id")],
            sorted([(f"chat-{chat_a}", a, chat_a), (f"chat-{chat_b}", b, chat_b)]))
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM orchs").fetchone()[0], 2)

    def test_first_binding_waits_for_new_job_to_publish_pid_and_status(self):
        chat = self.chat()
        turn = self.user_turn(chat, "first turn")
        read_jobs = self.rt.live_jobs
        starting = True

        def live_jobs(timeout=None):
            nonlocal starting
            jobs = read_jobs(timeout)
            if starting and any(job.startswith("job-o") for job in jobs):
                starting = False
                return {job: {} for job in jobs}
            return jobs

        with patch.object(self.rt, "live_jobs", side_effect=live_jobs):
            result = self.data(self.desktop(chat, turn, "status"))
        self.assertTrue(result["started_new_orch"])
        self.assertEqual(result["orch_health"]["state"], "alive")
        self.assertFalse(starting)

    def test_simultaneous_first_calls_start_only_one_orch_for_the_same_chat(self):
        chat = self.chat()
        turn = self.user_turn(chat, "first turn")
        with ThreadPoolExecutor(max_workers=2) as pool:
            requests = [pool.submit(self.desktop, chat, turn, "status") for _ in range(2)]
            results = [self.data(request.result(timeout=15)) for request in requests]
        self.assertEqual(results[0]["orch_id"], results[1]["orch_id"])
        self.assertEqual(sum(result["started_new_orch"] for result in results), 1)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM orchs").fetchone()[0], 1)

    def test_unverified_or_conflicting_context_cannot_create_or_use_a_binding(self):
        chat = self.chat()
        turn = self.user_turn(chat, "known")
        other = self.chat()
        foreign = self.chat(cwd=self.root / "another-interface")
        cli = self.chat(originator="codex_cli_rs")
        self.data(self.desktop(chat, turn, "status"))
        for claimed, seen in ((chat, str(uuid4())), (other, turn),
                              (foreign, self.user_turn(foreign, "wrong project")),
                              (cli, self.user_turn(cli, "wrong app"))):
            with self.subTest(claimed=claimed, seen=seen):
                self.assertTrue(self.desktop(claimed, seen, "relay").get("isError"))
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM orchs").fetchone()[0], 1)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM entry_messages").fetchone()[0], 0)

    def test_init_only_enables_per_chat_on_a_new_or_already_configured_interface(self):
        with self.assertRaisesRegex(ValueError, "use a new Interface folder"):
            bootstrap.init(self.rt, home=self.root, env=self.env, trust=False, entry_per_thread=True)
        self.config.unlink()
        bootstrap.init(self.rt, home=self.root, env=self.env, trust=False, entry_per_thread=True)
        bootstrap.init(self.rt, home=self.root, env=self.env, trust=False, entry_per_thread=True)
        listed = subprocess.run(
            [sys.executable, "-m", "orchd.cli", "mcp", "--role", "entry", "--entry-per-thread"],
            input=json.dumps({"id": 1, "method": "tools/list"}) + "\n",
            text=True, capture_output=True, timeout=15, check=True)
        tools = json.loads(listed.stdout)["result"]["tools"]
        self.assertEqual({tool["name"] for tool in tools}, {"status", "relay", "foreground"})
        entry_settings = tomllib.loads(self.config.read_text(encoding="utf-8"))["mcp_servers"]["orchd_entry"]
        self.assertEqual(entry_settings["args"][-4:], ["mcp", "--role", "entry", "--entry-per-thread"])
        self.assertEqual(set(entry_settings["tools"]), {"status", "relay", "foreground"})
        self.assertEqual(entry_settings["env"]["ORCHD_HOME"], self.env["ORCHD_HOME"])
