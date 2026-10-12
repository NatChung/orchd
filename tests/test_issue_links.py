import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from orchd import cli, core, issue_links, mcp_server, store
from test_orchd import FakeRuntime


class IssueLinkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "orchd.db"
        self.con = store.connect(self.path)
        self.addCleanup(self.con.close)
        self.rt = FakeRuntime()
        self.task = self.dispatch()
        self.tid = self.task["id"]

    def dispatch(self, **options):
        return core.dispatch(self.con, self.rt, orch_thread="synthetic-orch", repo="example",
                             title="Synthetic task", instructions="Synthetic instructions", done_when="Synthetic check",
                             model_reason="Synthetic test", task_type="code", **options)

    def link(self, ref="github:example/project#1", **options):
        return issue_links.link(self.con, self.tid, ref, **options)

    def test_link_supersede_unlink_preserves_task_and_schema(self):
        before_task = dict(store.get_task(self.con, self.tid))
        before_schema = list(self.con.execute("SELECT sql FROM sqlite_master ORDER BY name"))
        first = self.link(actor="operator", idempotency_key="first")
        second = self.link("jira:TEST-2", idempotency_key="second")
        third = issue_links.unlink(self.con, self.tid, idempotency_key="third")
        self.assertEqual(first["supersedes"], None)
        self.assertEqual(second["supersedes"], first["message_id"])
        self.assertEqual(third["supersedes"], second["message_id"])
        self.assertEqual(third["status"], "unlinked")
        self.assertTrue(all(r["changed"] for r in (first, second, third)))
        events = issue_links.history(self.con, self.tid)
        self.assertEqual([r["ref"] for r in events], ["github:example/project#1", "jira:TEST-2", None])
        self.assertEqual([r["version"] for r in events], [1, 1, 1])
        self.assertEqual(events[0]["actor"], "operator")
        self.assertEqual(dict(store.get_task(self.con, self.tid)), before_task)
        self.assertEqual(list(self.con.execute("SELECT sql FROM sqlite_master ORDER BY name")), before_schema)

    def test_durable_replay_after_superseding_does_not_restore_old_ref(self):
        receipt = self.link(idempotency_key="replay")
        self.link("jira:TEST-2")
        self.con.close()
        self.con = store.connect(self.path)
        self.addCleanup(self.con.close)
        self.assertEqual(self.link(idempotency_key="replay"), receipt)
        self.assertEqual(len(issue_links.history(self.con, self.tid)), 2)
        self.assertEqual(issue_links.history(self.con, self.tid)[-1]["ref"], "jira:TEST-2")
        unlink = issue_links.unlink(self.con, self.tid, idempotency_key="unlink")
        self.assertEqual(issue_links.unlink(self.con, self.tid, idempotency_key="unlink"), unlink)
        for ref, actor in (("jira:TEST-3", "orch"), ("github:example/project#1", "operator"), (None, "orch")):
            with self.assertRaisesRegex(ValueError, "idempotency conflict"):
                issue_links.set_ref(self.con, self.tid, ref, actor=actor, idempotency_key="replay")
        self.assertEqual(len(issue_links.history(self.con, self.tid)), 3)

    def test_generated_key_replay_no_change_and_closed_task_supported(self):
        first = self.link()
        self.assertEqual(self.link(idempotency_key=first["idempotency_key"]), first)
        second = self.link()
        self.assertFalse(second["changed"])
        store.update_task(self.con, self.tid, status="closed")
        issue_links.unlink(self.con, self.tid)
        self.assertFalse(issue_links.unlink(self.con, self.tid)["changed"])
        self.assertEqual(store.get_task(self.con, self.tid)["status"], "closed")

    def test_invalid_inputs_and_unknown_task_never_write(self):
        for ref in (None, "", "github:a/b#0", "github:a/b#-1", "github:a/b#01", "github:a/b#1\n",
                    "github:a/b/c#1", "https://github.com/a/b/issues/1", "jira:sf-1", "jira:SF-0", "jira:SF-1 ", 42):
            with self.subTest(ref=ref), self.assertRaises(ValueError):
                self.link(ref)
        for options in ({"actor": "dashboard"}, {"idempotency_key": " "}, {"idempotency_key": 1}):
            with self.assertRaises(ValueError):
                self.link(**options)
        with self.assertRaises(KeyError):
            issue_links.link(self.con, "missing", "jira:TEST-1")
        with self.assertRaises(KeyError):
            issue_links.unlink(self.con, "missing")
        self.assertEqual(issue_links.history(self.con), [])

    def test_concurrent_same_key_one_event_and_concurrent_changes_form_chain(self):
        def run(refs, key):
            barrier = threading.Barrier(2)
            results, errors = [], []
            def write(ref):
                con = store.connect(self.path)
                try:
                    barrier.wait()
                    results.append(issue_links.link(con, self.tid, ref, idempotency_key=key))
                except Exception as error:
                    errors.append(error)
                finally:
                    con.close()
            threads = [threading.Thread(target=write, args=(ref,)) for ref in refs]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(10)
                self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            return results
        receipts = run(["jira:TEST-1"] * 2, "concurrent")
        self.assertEqual(receipts[0], receipts[1])
        self.assertEqual(len(issue_links.history(self.con)), 1)
        run(["jira:TEST-2", "jira:TEST-3"], None)
        history = issue_links.history(self.con)
        self.assertEqual(len(history), 3)
        self.assertEqual([r["supersedes"] for r in history], [None, history[0]["message_id"], history[1]["message_id"]])

    def test_cli_json_receipts_and_errors(self):
        def call(*arguments):
            out = io.StringIO()
            with patch.dict(os.environ, ORCHD_HOME=self.temp.name), contextlib.redirect_stdout(out):
                code = cli.main(["task", *arguments])
            return code, json.loads(out.getvalue())
        args = ("link", "--task", self.tid, "--ref", "jira:TEST-1", "--actor", "operator", "--idempotency-key", "cli")
        code, receipt = call(*args)
        self.assertEqual(code, 0)
        self.assertEqual(call(*args), (code, receipt))
        self.assertEqual(call("unlink", "--task", self.tid)[1]["issue_ref"], None)
        for tid, ref in ((self.tid, "bad"), ("missing", "jira:TEST-1")):
            code, failure = call("link", "--task", tid, "--ref", ref)
            self.assertEqual((code, failure["status"]), (1, "failed"))

    def test_dispatch_optional_ref_mcp_tools_and_failure_do_not_link(self):
        dispatch_schema = next(t for t in mcp_server.TOOLS if t["name"] == "dispatch")["inputSchema"]
        self.assertIn("issue_ref", dispatch_schema["properties"])
        self.assertNotIn("issue_ref", dispatch_schema["required"])
        for ref in ("github:example/project#1", "jira:TEST-2"):
            result = mcp_server.call("dispatch", dict(repo="example", title="Synthetic", instructions="Synthetic",
                                     done_when="Synthetic", model_reason="Synthetic", task_type="code", issue_ref=ref),
                                     "synthetic-orch", self.con, self.rt)
            self.assertEqual(issue_links.history(self.con, result["task_id"])[0]["ref"], ref)
        self.assertEqual(issue_links.history(self.con, self.tid), [])
        before = self.con.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        with self.assertRaises(ValueError):
            self.dispatch(issue_ref="bad")
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], before)
        with patch.object(self.rt, "start_codex_worker", side_effect=RuntimeError("synthetic launch failure")):
            with self.assertRaises(RuntimeError):
                self.dispatch(issue_ref="jira:TEST-1")
        failed = self.con.execute("SELECT id FROM tasks WHERE status='failed'").fetchone()[0]
        self.assertEqual(issue_links.history(self.con, failed), [])
        result = mcp_server.call("task_link", dict(task_id=self.tid, ref="jira:TEST-4", idempotency_key="mcp"),
                                 "synthetic-orch", self.con, self.rt)
        self.assertEqual(result["actor"], "orch")
        self.assertEqual(mcp_server.call("task_unlink", dict(task_id=self.tid), "synthetic-orch", self.con, self.rt)["issue_ref"], None)
        with self.assertRaises(ValueError):
            mcp_server.call("task_link", dict(task_id=self.tid, ref="jira:TEST-5"), None, self.con, self.rt)
