import io
import json
import tempfile
import unittest
from pathlib import Path

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


class ListOpenPageTest(unittest.TestCase):
    def call(self, args, count):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        con = store.connect(Path(tmp.name) / "t.db")
        rt = FakeRuntime()
        for i in range(count):
            core.dispatch(con, rt, orch_thread="thread-A", repo="demo", title=f"T{i}", instructions="i",
                          done_when="d", model_reason="r", task_type="code")
        out = io.StringIO()
        message = dict(jsonrpc="2.0", id=1, method="tools/call", params=dict(name="list_open", arguments=args))
        mcp_server.serve(io.StringIO(json.dumps(message) + "\n"), out, con, rt)
        return json.loads(json.loads(out.getvalue())["result"]["content"][0]["text"])

    def test_oversized_list_is_bounded_with_total_and_next_step_hint(self):
        result = self.call({"limit": 2}, 5)
        self.assertEqual((result["total"], result["showing"], result["truncated"]), (5, 2, True))
        self.assertEqual(len(result["tasks"]), 2)
        self.assertIn("limit", result["hint"])

    def test_default_limit_applies_and_small_list_is_not_truncated(self):
        result = self.call({}, 3)
        self.assertEqual((result["total"], result["showing"], result["truncated"]), (3, 3, False))
        self.assertNotIn("hint", result)

    def test_bad_limit_is_refused(self):
        with self.assertRaises(ValueError):
            limits.page([1], 0)


class IntakePendingBytesTest(IntakeBase):
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
