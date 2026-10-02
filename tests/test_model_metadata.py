import os
import tempfile
import unittest
from unittest import mock

from orchd import core, store
from tests.test_orchd import FakeRuntime


class ModelMetadataTest(unittest.TestCase):
    """inbox / list_open expose each task's stored full model id. Temporary ORCHD_HOME only."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.dict(os.environ, {"ORCHD_HOME": self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.con = store.connect()
        self.rt = FakeRuntime()

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def dispatch(self, thread="thread-A", **kw):
        kw = {"model_reason": "clear scope", "task_type": "code", **kw}
        return core.dispatch(self.con, self.rt, orch_thread=thread, repo="demo", title="T",
                             instructions="do it", done_when="tests pass", **kw)

    def test_every_message_kind_carries_the_task_stored_model(self):
        t = self.dispatch()
        stored = store.get_task(self.con, t["id"])["model"]
        core.ack(self.con, t["id"])
        core.progress(self.con, self.rt, t["id"], "halfway")
        core.ask(self.con, self.rt, t["id"], "which one?")
        core.report(self.con, self.rt, t["id"], "done", "ok", "ev")
        msgs = core.inbox(self.con, "thread-A")
        self.assertEqual([m["kind"] for m in msgs], ["ack", "progress", "question", "report"])
        self.assertTrue(all(m["model"] == stored for m in msgs))
        for key in ("task_id", "repo", "title", "kind", "body", "evidence", "task_status"):
            self.assertIn(key, msgs[0])

    def test_model_is_the_full_stored_id_not_the_alias(self):
        for alias in core.MODELS:
            t = self.dispatch(model=alias)
            self.assertEqual(self.row(t["id"])["model"], core.MODELS[alias])

    def row(self, task_id):
        return next(r for r in core.list_open(self.con, self.rt) if r["task_id"] == task_id)

    def test_list_open_keeps_existing_fields_and_does_not_consume_inbox(self):
        t = self.dispatch()
        core.ack(self.con, t["id"])
        row = self.row(t["id"])
        for key in ("owner_health", "notification_delivery", "worker_health", "recovery_hint", "worker_alive"):
            self.assertIn(key, row)
        self.assertEqual(len(core.inbox(self.con, "thread-A")), 1)  # list_open left it unread

    def test_legacy_null_and_empty_model_is_unknown(self):
        for legacy in (None, "", "  "):
            t = self.dispatch()
            self.con.execute("UPDATE tasks SET model=? WHERE id=?", (legacy, t["id"]))
            core.ack(self.con, t["id"])
            self.assertEqual(self.row(t["id"])["model"], "unknown")
        msgs = core.inbox(self.con, "thread-A")
        self.assertEqual({m["model"] for m in msgs}, {"unknown"})

    def test_other_orch_messages_are_not_consumed(self):
        a, b = self.dispatch("thread-A"), self.dispatch("thread-B")
        core.ack(self.con, a["id"])
        core.ack(self.con, b["id"])
        self.assertEqual([m["task_id"] for m in core.inbox(self.con, "thread-A")], [a["id"]])
        self.assertEqual([m["task_id"] for m in core.inbox(self.con, "thread-B")], [b["id"]])

    def test_mcp_descriptions_document_model(self):
        from orchd import mcp_server
        tools = {t["name"]: t["description"] for t in mcp_server.TOOLS}
        self.assertIn("model", tools["inbox"])
        self.assertIn("model", tools["list_open"])
