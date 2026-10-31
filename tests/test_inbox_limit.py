"""Issue #93: inbox has a limit, never loses messages, puts questions and open tasks first, caps body size."""
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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
        self.assertEqual(bodies, ["live-progress", "live-question", "closed-question"])  # shown oldest first
        self.assertEqual([r["message_id"] for r in page], sorted(r["message_id"] for r in page))
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
        big = "x" * (core.INBOX_CAPS["body"] + 500)
        mid = store.add_message(self.con, t["id"], "report", big)
        row = [r for r in core.inbox(self.con, "thread-A") if r["kind"] == "report"][0]
        self.assertLess(len(row["body"]), len(big))
        self.assertIn(f"truncated, {len(big)} chars", row["body"])
        self.assertEqual(row["message_id"], mid)
        full = core.inbox_message(self.con, "thread-A", mid)
        self.assertEqual(full["text"], big)
        self.assertIsNone(full["next_offset"])
        with self.assertRaises(ValueError):
            core.inbox_message(self.con, "thread-B", mid)

    def mcp(self, name, arguments=None, sent=None):
        msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments or {}}}
        with mock.patch.dict("os.environ", {"ORCHD_ORCH_ID": "thread-A"}):
            return mcp_server.handle(msg, self.con, self.rt, "orch", sent=sent)

    def mcp_rows(self, reply):
        return json.loads(reply["result"]["content"][0]["text"])

    def big_messages(self, n, kind="report", size=None):
        t = self.dispatch("T" * 500)
        store.update_task(self.con, t["id"], model_reason="m" * 5000)
        size = size or core.INBOX_CAPS["body"] * 3
        ids = [store.add_message(self.con, t["id"], kind, "y" * size, "e" * 20_000) for _ in range(n)]
        return t, ids

    def test_schema_documents_limit_budget_and_slices(self):
        tool = [x for x in mcp_server.TOOLS if x["name"] == "inbox"][0]
        props = tool["inputSchema"]["properties"]
        for key in ("limit", "field", "offset", "length"):
            self.assertIn(key, props)
        for word in ("remaining", "remaining_closed", "at-least-once", "60 KB"):
            self.assertIn(word, tool["description"])

    def test_166k_case_page_fits_the_budget_through_the_mcp_server_and_the_rest_stays_unread(self):
        self.big_messages(core.INBOX_DEFAULT_LIMIT * 3)
        total = self.unread()
        reply = self.mcp("inbox")
        text = reply["result"]["content"][0]["text"]
        self.assertLessEqual(len(text.encode("utf-8")), core.INBOX_PAGE_BUDGET)
        rows = json.loads(text)
        self.assertGreater(len(rows), 0)
        self.assertLessEqual(len(rows), core.INBOX_DEFAULT_LIMIT)
        for r in rows:
            for f, cap in core.INBOX_CAPS.items():
                self.assertLess(len(r[f]), cap + 200, f)
        self.assertEqual(rows[0]["remaining"], total - len(rows))
        self.assertEqual(self.unread(), total - len(rows))

    def test_budget_not_limit_decides_the_page_and_whole_backlog_drains(self):
        _, ids = self.big_messages(30)
        seen = []
        for _ in range(100):
            rows = self.mcp_rows(self.mcp("inbox", {"limit": 100}))
            if not rows:
                break
            self.assertLess(len(rows), 30)  # the byte budget cut the page, not the limit
            seen += [r["message_id"] for r in rows]
        self.assertEqual([i for i in seen if i in ids], ids)
        self.assertEqual(self.unread(), 0)

    def test_single_oversized_message_is_previewed_and_readable_in_slices(self):
        t = self.dispatch("T" * 5000)
        body = "".join(chr(0x4e00 + i % 500) for i in range(70_000))
        mid = store.add_message(self.con, t["id"], "report", body, "E" * 50_000)
        rows = [r for r in core.inbox(self.con, "thread-A") if r["message_id"] == mid]
        self.assertEqual(len(rows), 1)
        self.assertLessEqual(core._inbox_size(rows), core.INBOX_PAGE_BUDGET)
        for field, full in (("body", body), ("evidence", "E" * 50_000), ("title", "T" * 5000)):
            got, offset = "", 0
            while offset is not None:
                part = core.inbox_message(self.con, "thread-A", mid, field, offset)
                self.assertLessEqual(len(part["text"]), core.INBOX_READ_LENGTH)
                got += part["text"]
                offset = part["next_offset"]
            self.assertEqual(got, full)

    def test_cjk_page_is_measured_in_bytes(self):
        t = self.dispatch()
        for _ in range(15):
            store.add_message(self.con, t["id"], "report", "字" * 5000, "證" * 5000)
        reply = self.mcp("inbox")
        self.assertLessEqual(len(reply["result"]["content"][0]["text"].encode("utf-8")), core.INBOX_PAGE_BUDGET)

    def test_assembly_failure_does_not_mark_anything_read(self):
        t = self.dispatch()
        for i in range(3):
            store.add_message(self.con, t["id"], "progress", f"p{i}")
        before = self.unread()
        with mock.patch.object(core, "task_model", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                core.inbox(self.con, "thread-A")
            reply = self.mcp("inbox")
        self.assertTrue(reply["result"]["isError"])
        self.assertEqual(self.unread(), before)
        self.assertEqual(len(core.inbox(self.con, "thread-A")), before)

    def test_serialization_failure_does_not_mark_read(self):
        t = self.dispatch()
        store.add_message(self.con, t["id"], "progress", "p")
        before = self.unread()
        with mock.patch.object(mcp_server.json, "dumps", side_effect=TypeError("nope")):
            reply = self.mcp("inbox")
        self.assertTrue(reply["result"]["isError"])
        self.assertEqual(self.unread(), before)

    def test_transport_failure_leaves_messages_unread_for_retry(self):
        t = self.dispatch()
        store.add_message(self.con, t["id"], "progress", "p")
        before = self.unread()

        class Broken(io.StringIO):
            def write(self, _):
                raise BrokenPipeError

        request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": "inbox", "arguments": {}}}) + "\n"
        with mock.patch.dict("os.environ", {"ORCHD_ORCH_ID": "thread-A"}), mock.patch.object(mcp_server.quota, "start_background"), \
                mock.patch.object(mcp_server.quota, "stop_background"):
            with self.assertRaises(BrokenPipeError):
                mcp_server.serve(io.StringIO(request), Broken(), self.con, self.rt)
            self.assertEqual(self.unread(), before)
            out = io.StringIO()
            mcp_server.serve(io.StringIO(request), out, self.con, self.rt)
        rows = json.loads(json.loads(out.getvalue())["result"]["content"][0]["text"])
        self.assertEqual(len(rows), before)
        self.assertEqual(self.unread(), 0)  # marked once the reply was written

    def test_page_is_marked_read_after_a_successful_mcp_call_and_empty_inbox_is_an_empty_list(self):
        t = self.dispatch()
        store.add_message(self.con, t["id"], "progress", "p")
        before = self.unread()
        self.assertEqual(len(self.mcp_rows(self.mcp("inbox"))), before)
        self.assertEqual(self.unread(), 0)
        self.assertEqual(self.mcp_rows(self.mcp("inbox")), [])
        self.assertEqual(core.inbox(self.con, "thread-A"), [])

    def test_many_questions_fill_pages_first_and_all_are_eventually_read(self):
        t = self.dispatch()
        qs = [store.add_message(self.con, t["id"], "question", "q" * 5000, "e" * 5000) for _ in range(40)]
        progress = store.add_message(self.con, t["id"], "progress", "later")
        got = []
        for _ in range(100):
            rows = core.inbox(self.con, "thread-A", limit=100)
            if not rows:
                break
            got += [r["message_id"] for r in rows]
        self.assertEqual(got[:len(qs)], qs)  # questions come before the progress message
        self.assertIn(progress, got)

    def test_closed_messages_can_starve_but_remaining_counters_say_so_and_draining_reads_them(self):
        old = self.dispatch("old")
        closed_ids = [store.add_message(self.con, old["id"], "progress", f"old{i}") for i in range(3)]
        store.update_task(self.con, old["id"], status="closed")
        live = self.dispatch("live")
        for i in range(core.INBOX_DEFAULT_LIMIT):
            store.add_message(self.con, live["id"], "question", f"q{i}")
        first = core.inbox(self.con, "thread-A")
        self.assertFalse(any(r["message_id"] in closed_ids for r in first))
        self.assertGreaterEqual(first[0]["remaining_closed"], 3)
        self.assertEqual(first[0]["remaining"], first[0]["remaining_priority"] + first[0]["remaining_closed"])
        store.add_message(self.con, live["id"], "question", "newer question")  # keeps priority work coming
        second = core.inbox(self.con, "thread-A")
        self.assertTrue(any(r["body"] == "newer question" for r in second))
        seen = {r["message_id"] for r in second}
        for _ in range(20):
            rows = core.inbox(self.con, "thread-A")
            if not rows:
                break
            seen |= {r["message_id"] for r in rows}
        self.assertTrue(set(closed_ids) <= seen)

    def test_inbox_message_only_reads_orch_kinds_of_the_callers_own_tasks(self):
        t = self.dispatch()
        hidden = [store.add_message(self.con, t["id"], kind, "secret") for kind in ("answer_queued", "answer", "dispatch")]
        shown = store.add_message(self.con, t["id"], "progress", "ok")
        for mid in hidden:
            with self.assertRaises(ValueError):
                core.inbox_message(self.con, "thread-A", mid)
            with self.assertRaises(ValueError):
                core.inbox_message(self.con, "thread-B", mid)
        self.assertEqual(core.inbox_message(self.con, "thread-A", shown)["text"], "ok")
        with self.assertRaises(ValueError):
            core.inbox_message(self.con, "thread-B", shown)
        self.assertIn(shown, [r["message_id"] for r in core.inbox(self.con, "thread-A")])
        self.assertTrue(set(hidden).isdisjoint(r["message_id"] for r in core.inbox(self.con, "thread-A", limit=100)))

    def test_inbox_message_is_read_only_and_validates_arguments(self):
        t = self.dispatch()
        mid = store.add_message(self.con, t["id"], "progress", "abc")
        before = self.unread()
        core.inbox_message(self.con, "thread-A", mid)
        self.assertEqual(self.unread(), before)
        for bad in (dict(field="kind"), dict(offset=-1), dict(length=0), dict(offset=True)):
            with self.assertRaises(ValueError):
                core.inbox_message(self.con, "thread-A", mid, **bad)

if __name__ == "__main__":
    unittest.main()
