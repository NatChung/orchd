"""Issue #93: inbox has a limit, never loses messages, puts questions and open tasks first, caps body size."""
import tempfile
import unittest
from pathlib import Path

from orchd import core, mcp_server, store
from tests.test_orchd import FakeRuntime


class InboxLimitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = store.connect(Path(self.tmp.name) / "t.db")
        self.rt = FakeRuntime()

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def dispatch(self, title="T", group_id=None):
        return core.dispatch(self.con, self.rt, orch_thread="thread-A", repo="demo", title=title, instructions="do it",
                             done_when="ok", model="sol", model_reason="r", task_type="code", backend="exec")

    def unread(self):
        return len(store.unread_for_thread(self.con, "thread-A"))

    def test_more_unread_than_limit_returns_limit_and_keeps_the_rest_unread(self):
        t = self.dispatch()
        for i in range(7):
            store.add_message(self.con, t["id"], "progress", f"p{i}")
        before = self.unread()
        first = core.inbox(self.con, "thread-A", limit=3)
        self.assertEqual(len(first), 3)
        self.assertEqual({r["remaining"] for r in first}, {before - 3})
        self.assertEqual(self.unread(), before - 3)
        seen = [r["body"] for r in first]
        while True:
            page = core.inbox(self.con, "thread-A", limit=3)
            if not page:
                break
            seen += [r["body"] for r in page]
        self.assertEqual([b for b in seen if b.startswith("p")], [f"p{i}" for i in range(7)])
        self.assertEqual(self.unread(), 0)

    def test_default_limit_applies_and_nothing_is_lost(self):
        t = self.dispatch()
        for i in range(core.INBOX_DEFAULT_LIMIT + 5):
            store.add_message(self.con, t["id"], "progress", f"p{i}")
        total = self.unread()
        page = core.inbox(self.con, "thread-A")
        self.assertEqual(len(page), core.INBOX_DEFAULT_LIMIT)
        self.assertEqual(page[0]["remaining"], total - core.INBOX_DEFAULT_LIMIT)
        self.assertEqual(len(core.inbox(self.con, "thread-A")), total - core.INBOX_DEFAULT_LIMIT)

    def test_questions_and_open_tasks_are_not_pushed_out_by_old_closed_task_messages(self):
        old = self.dispatch("old")
        for i in range(10):
            store.add_message(self.con, old["id"], "progress", f"old{i}")
        store.update_task(self.con, old["id"], status="closed")
        live = self.dispatch("live")
        store.add_message(self.con, live["id"], "progress", "live-progress")
        store.add_message(self.con, live["id"], "question", "live-question")
        closed_q = self.dispatch("closed-with-question")
        store.add_message(self.con, closed_q["id"], "question", "closed-question")
        store.update_task(self.con, closed_q["id"], status="closed")
        page = core.inbox(self.con, "thread-A", limit=3)
        bodies = [r["body"] for r in page]
        self.assertIn("live-question", bodies)
        self.assertIn("closed-question", bodies)
        self.assertIn("live-progress", bodies)
        self.assertEqual(bodies, sorted(bodies, key=lambda b: [r["body"] for r in page].index(b)))
        self.assertFalse(any(b.startswith("old") for b in bodies))
        self.assertEqual(page[0]["remaining"], self.unread())
        self.assertGreater(page[0]["remaining"], 0)
        rest = [r["body"] for r in core.inbox(self.con, "thread-A", limit=100)]
        self.assertEqual(sum(b.startswith("old") for b in rest), 10)

    def test_group_filter_with_limit_keeps_other_groups_unread(self):
        a = self.dispatch("a")
        b = self.dispatch("b")
        store.update_task(self.con, a["id"], group_id="g1")
        store.update_task(self.con, b["id"], group_id="g2")
        for i in range(4):
            store.add_message(self.con, a["id"], "progress", f"a{i}")
        store.add_message(self.con, b["id"], "progress", "b0")
        b_before = len([r for r in store.unread_for_thread(self.con, "thread-A") if r["group_id"] == "g2"])
        page = core.inbox(self.con, "thread-A", group_id="g1", limit=2)
        self.assertEqual(len(page), 2)
        self.assertTrue(all(r["group_id"] == "g1" for r in page))
        self.assertEqual(page[0]["remaining"], len([r for r in store.unread_for_thread(self.con, "thread-A") if r["group_id"] == "g1"]))
        self.assertEqual(len([r for r in store.unread_for_thread(self.con, "thread-A") if r["group_id"] == "g2"]), b_before)

    def test_long_body_is_truncated_with_a_marker_and_full_text_stays_readable(self):
        t = self.dispatch()
        big = "x" * (core.INBOX_BODY_CAP + 500)
        mid = store.add_message(self.con, t["id"], "report", big)
        row = [r for r in core.inbox(self.con, "thread-A") if r["kind"] == "report"][0]
        self.assertLess(len(row["body"]), len(big))
        self.assertIn(f"truncated, {len(big)} chars", row["body"])
        self.assertEqual(row["message_id"], mid)
        full = core.inbox_message(self.con, "thread-A", mid)
        self.assertEqual(full["body"], big)
        with self.assertRaises(ValueError):
            core.inbox_message(self.con, "thread-B", mid)

    def test_mcp_default_output_stays_under_the_tool_limit_and_schema_documents_limit(self):
        t = self.dispatch()
        for i in range(core.INBOX_DEFAULT_LIMIT * 3):
            store.add_message(self.con, t["id"], "report", "y" * (core.INBOX_BODY_CAP * 3))
        import json
        out = json.dumps(core.inbox(self.con, "thread-A"))
        self.assertLess(len(out), 100_000)
        tool = [x for x in mcp_server.TOOLS if x["name"] == "inbox"][0]
        self.assertIn("limit", tool["inputSchema"]["properties"])
        self.assertIn("remaining", tool["description"])


if __name__ == "__main__":
    unittest.main()
