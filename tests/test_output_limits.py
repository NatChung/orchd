import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchd import core, intake, limits, mcp_server, store
from tests.test_intake import IntakeBase
from tests.test_orchd import FakeRuntime
from tests.test_retry import RetryRuntime


class ModelReasonBoundTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.con = store.connect(Path(self.tmp.name) / "t.db")
        self.rt = RetryRuntime()

    def dispatch(self):
        t = core.dispatch(self.con, self.rt, orch_thread="thread-A", repo="demo", title="T", instructions="do it",
                          done_when="tests pass", model="sonnet", model_reason="clear scope", task_type="code",
                          backend="exec")
        self.rt.started.clear()
        return t

    def events(self, task_id, kind):
        return [json.loads(r["body"]) for r in self.con.execute(
            "SELECT body FROM messages WHERE task_id=? AND kind=? ORDER BY id", (task_id, kind))]

    def test_ten_retries_keep_model_reason_bounded_and_history_available(self):
        t = self.dispatch()
        for i in range(10):
            core.retry(self.con, self.rt, t["id"], "sonnet", f"attempt {i} " + "x" * 400)
        self.assertEqual(store.get_task(self.con, t["id"])["model_reason"], "clear scope")  # first reason only
        (row,) = core.list_open(self.con, self.rt)
        self.assertEqual(row["model_reason"], "clear scope")
        self.assertEqual(row["retry_count"], 10)
        self.assertTrue(row["last_retry"]["reason"].startswith("attempt 9 "))
        self.assertLessEqual(len(json.dumps(row)), 3000)
        self.assertEqual(len(self.events(t["id"], "retry")), 10)  # history stays in the events

    def test_legacy_long_model_reason_is_capped_on_output_not_in_the_db(self):
        t = self.dispatch()
        store.update_task(self.con, t["id"], model_reason="r" * 5000)
        (row,) = core.list_open(self.con, self.rt)
        self.assertLess(len(row["model_reason"]), limits.TEXT_CAP + 60)
        self.assertIn("truncated", row["model_reason"])
        self.assertEqual(len(store.get_task(self.con, t["id"])["model_reason"]), 5000)

    def test_inbox_caps_model_reason(self):
        t = self.dispatch()
        store.update_task(self.con, t["id"], model_reason="r" * 5000)
        core.report(self.con, self.rt, t["id"], "done", "ok", "")
        (msg,) = [m for m in core.inbox(self.con, "thread-A") if m["kind"] == "report"]
        self.assertIn("truncated", msg["model_reason"])


SAMPLES = {"ascii": "a", "cjk": "漢", "quote": '"', "control": "\x01\n\t\\", "emoji": "😀"}


def wire(handled):
    """What the client actually reads: the one-line JSON-RPC reply, serialized the way serve() writes it."""
    return len(json.dumps(handled, ensure_ascii=False).encode())


def tool_call(con, rt, name, args, thread="thread-A"):
    message = dict(jsonrpc="2.0", id=1, method="tools/call", params=dict(name=name, arguments=args))
    with mock.patch.dict(os.environ, {"ORCHD_ORCH_ID": thread}):
        handled = mcp_server.handle(message, con, rt)
    assert "isError" not in handled["result"], handled["result"]
    return handled, json.loads(handled["result"]["content"][0]["text"])


class ListOpenWireTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.con = store.connect(Path(self.tmp.name) / "t.db")
        self.addCleanup(self.con.close)
        self.rt = FakeRuntime()

    def dispatch(self, count, title):
        return [core.dispatch(self.con, self.rt, orch_thread="thread-A", repo="demo", title=title(i), instructions="i",
                              done_when="d", model_reason="r", task_type="code")["id"] for i in range(count)]

    def walk(self, args=None):
        """Follow next_cursor to the end; returns (pages, task ids read) with every reply's wire size checked."""
        args, pages, seen = dict(args or {}), [], []
        while True:
            handled, data = tool_call(self.con, self.rt, "list_open", args)
            self.assertLessEqual(wire(handled), limits.MAX_PAGE_BYTES)
            rows = data if isinstance(data, list) else data["tasks"]
            pages.append(data)
            seen += [t["task_id"] for t in rows]
            if isinstance(data, list) or not data.get("next_cursor"):
                return pages, seen
            args["cursor"] = data["next_cursor"]

    def test_small_list_stays_a_bare_list(self):
        ids = self.dispatch(3, lambda i: f"T{i}")
        _, data = tool_call(self.con, self.rt, "list_open", {})
        self.assertIsInstance(data, list)
        self.assertEqual([t["task_id"] for t in data], ids)

    def test_with_counts_is_always_a_dict(self):
        self.dispatch(2, lambda i: f"T{i}")
        _, data = tool_call(self.con, self.rt, "list_open", {"with_counts": True})
        self.assertEqual((data["total"], data["shown"], data["showing"], data["truncated"]), (2, 2, 2, False))

    def test_final_wire_stays_within_budget_for_every_character_class(self):
        for name, ch in SAMPLES.items():
            with self.subTest(name):
                self.setUp()
                ids = self.dispatch(50, lambda i: ch * 400 + str(i))
                handled, _ = tool_call(self.con, self.rt, "list_open", {})
                self.assertLessEqual(wire(handled), limits.MAX_PAGE_BYTES)
                pages, seen = self.walk()
                self.assertIsInstance(pages[0], dict)  # only an overflow turns the list into a page
                self.assertEqual(set(pages[0]) - {"hint"}, {"total", "shown", "showing", "truncated", "tasks", "next_cursor"})
                self.assertTrue(pages[0]["truncated"])
                self.assertEqual(seen, ids)  # nothing lost, nothing repeated

    def test_one_huge_title_is_shortened_and_the_cursor_still_advances(self):
        ids = self.dispatch(3, lambda i: ("T" * 1_400_000) if i == 0 else f"T{i}")
        handled, data = tool_call(self.con, self.rt, "list_open", {})
        self.assertLessEqual(wire(handled), limits.MAX_PAGE_BYTES)
        self.assertIn("truncated", data["tasks"][0]["title"])
        self.assertEqual(self.walk()[1], ids)

    def test_limit_pages_with_a_cursor(self):
        ids = self.dispatch(5, lambda i: f"T{i}")
        pages, seen = self.walk({"limit": 2})
        self.assertEqual([len(p["tasks"]) for p in pages], [2, 2, 1])
        self.assertEqual((pages[0]["total"], pages[0]["truncated"]), (5, True))
        self.assertFalse(pages[-1]["truncated"])
        self.assertEqual(seen, ids)

    def page(self, args):
        handled, page = tool_call(self.con, self.rt, "list_open", args)
        self.assertLessEqual(wire(handled), limits.MAX_PAGE_BYTES)
        return page

    def test_cursor_continues_after_cursor_task_closed(self):
        ids = self.dispatch(6, lambda i: f"T{i}")
        first = self.page({"limit": 2})
        core.close(self.con, self.rt, ids[1])
        core.close(self.con, self.rt, ids[3])  # an unseen closed task is skipped too
        core.close(self.con, self.rt, ids[5])  # the high-water task need not remain open either
        _, seen = self.walk({"limit": 2, "cursor": first["next_cursor"]})
        self.assertEqual(seen, [ids[2], ids[4]])

    def test_cursor_continues_after_leaving_attention_with_group_filter(self):
        ids = self.dispatch(5, lambda i: f"T{i}")
        for task_id in ids:
            store.update_task(self.con, task_id, status="question", group_id="selected")
        store.update_task(self.con, ids[3], group_id="other")
        args = {"limit": 1, "group_id": "selected", "attention_only": True}
        first = self.page(args)
        store.update_task(self.con, ids[0], status="starting")
        _, seen = self.walk(dict(args, cursor=first["next_cursor"]))
        self.assertEqual(seen, [ids[1], ids[2], ids[4]])

    def test_new_tasks_wait_for_next_walk_and_walk_terminates(self):
        ids = self.dispatch(4, lambda i: f"T{i}")
        args = {"limit": 1}
        seen, added = [], []
        for _ in range(8):  # bounded even if a broken cursor keeps chasing new arrivals
            page = self.page(args)
            seen.extend(t["task_id"] for t in page["tasks"])
            added += self.dispatch(1, lambda i: "new")
            if not page.get("next_cursor"):
                break
            self.assertIn("next", page["hint"])
            args["cursor"] = page["next_cursor"]
        else:
            self.fail("walk did not terminate while tasks were added")
        self.assertEqual(seen, ids)
        self.assertEqual(self.walk()[1], ids + added)

    def test_equal_created_at_uses_task_id_order_with_filters(self):
        with mock.patch.object(store, "new_task_id", side_effect=["cccccccc", "aaaaaaaa", "bbbbbbbb", "dddddddd"]):
            ids = self.dispatch(4, lambda i: f"T{i}")
        for task_id in ids:
            store.update_task(self.con, task_id, status="question", group_id="selected", created_at=1234)
        store.update_task(self.con, ids[3], group_id="other")
        args = {"limit": 1, "attention_only": True, "group_id": "selected"}
        self.assertEqual(self.walk(args)[1], sorted(ids[:3]))

    def test_malformed_cursors_have_clear_errors(self):
        for cursor in ("t1:aaaaaaaa", "t2:nope", 't2:[1,"aaaaaaaa"]',
                       't2:[true,"aaaaaaaa",2,"bbbbbbbb"]',
                       't2:[NaN,"aaaaaaaa",2,"bbbbbbbb"]',
                       't2:[3,"aaaaaaaa",2,"bbbbbbbb"]', 42):
            with self.subTest(cursor=cursor):
                handled = mcp_server.handle(dict(jsonrpc="2.0", id=1, method="tools/call",
                    params=dict(name="list_open", arguments={"cursor": cursor})), self.con, self.rt)
                self.assertTrue(handled["result"]["isError"])
                self.assertIn("invalid cursor", handled["result"]["content"][0]["text"])

    def test_bad_limit_and_unknown_cursor_are_refused(self):
        self.dispatch(1, lambda i: "T")
        with self.assertRaises(ValueError):
            limits.page([1], 0)
        with self.assertRaises(AssertionError):  # tool_call asserts the reply is not an error
            tool_call(self.con, self.rt, "list_open", {"limit": 0})
        with self.assertRaises(AssertionError):
            tool_call(self.con, self.rt, "list_open", {"cursor": "t1:nope"})


class IntakePendingBytesTest(IntakeBase):
    def setUp(self):
        super().setUp()
        store.register_orch(self.con, "thread-A", "claude")

    def pending(self, args=None):
        return tool_call(self.con, FakeRuntime(), "intake_pending", args or {})

    def walk(self, **args):
        """Follow next_cursor to the end, checking each reply's wire size; returns the event ids in order."""
        seen = []
        while True:
            handled, page = self.pending(args)
            self.assertLessEqual(wire(handled), limits.MAX_PAGE_BYTES)
            seen += [e["event_id"] for e in page["events"]]
            if not page["has_more"]:
                return seen
            args["cursor"] = page["next_cursor"]

    def own(self, count, make):
        ids = []
        for i in range(count):
            r = self.submit(event_id=f"ev-{i}", **make(i))
            intake.bind_owner(self.con, r["receipt_id"], "thread-A")
            ids.append(f"ev-{i}")
        return ids

    def test_final_wire_stays_within_budget_for_every_character_class(self):
        for name, ch in SAMPLES.items():
            with self.subTest(name):
                self.setUp()
                ids = self.own(100, lambda i: dict(summary=(ch * 900)[:900], source_ref=(ch * 100)[:100]))
                self.assertEqual(self.walk(), ids)  # no gap, no repeat

    def test_oversized_first_row_is_shortened_and_the_cursor_advances(self):
        ids = self.own(3, lambda i: dict(summary="s", source_ref=("R" * 1_400_000) if i == 0 else "ref"))
        handled, page = self.pending()
        self.assertLessEqual(wire(handled), limits.MAX_PAGE_BYTES)
        self.assertIn("truncated", page["events"][0]["source_ref"])
        self.assertEqual(self.walk(), ids)

    def test_page_is_cut_by_bytes_and_cursor_continues_without_loss(self):
        for i in range(6):
            self.submit(event_id=f"big-{i}", summary="s" * 900)
        first = intake.pending(self.con, None, 100, max_bytes=4000)
        self.assertTrue(0 < len(first["events"]) < 6)
        self.assertTrue(first["has_more"])
        seen = [e["event_id"] for e in first["events"]]
        page = first
        while page["has_more"]:
            page = intake.pending(self.con, None, 100, cursor=page["next_cursor"], max_bytes=4000)
            seen += [e["event_id"] for e in page["events"]]
        self.assertEqual(seen, [f"big-{i}" for i in range(6)])

    def test_one_event_always_fits_even_under_a_tiny_budget(self):
        for i in range(3):
            self.submit(event_id=f"huge-{i}", summary="s" * 1000)
        page = intake.pending(self.con, None, 100, max_bytes=100)
        self.assertEqual(len(page["events"]), 1)
        self.assertTrue(page["has_more"])
        self.assertTrue(page["next_cursor"])


if __name__ == "__main__":
    unittest.main()
