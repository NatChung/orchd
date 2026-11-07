import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from orchd import cli, core, store
from test_orchd import FakeRuntime


class DispatchCliTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        env = patch.dict(os.environ, {"ORCHD_HOME": temp.name, "ORCHD_CONFIG_DIR": temp.name,
                                     "ORCHD_ORCH_ID": "", "CODEX_THREAD_ID": ""})
        env.start()
        self.addCleanup(env.stop)
        self.con = store.connect()
        self.addCleanup(self.con.close)

    def test_standalone_shell_resolves_owner_without_generating_an_id(self):
        with self.assertRaisesRegex(ValueError, "select a registered Orch"):
            cli._caller_orch(self.con, None)
        store.register_orch(self.con, "registered", "codex")
        self.assertEqual(cli._caller_orch(self.con, None), "registered")
        store.register_orch(self.con, "another", "codex")
        with self.assertRaisesRegex(ValueError, "--orch-id"):
            cli._caller_orch(self.con, None)
        self.con.execute("INSERT INTO entries(id,orch_id,bound_at) VALUES('desktop','another',0)")
        self.assertEqual(cli._caller_orch(self.con, None), "another")
        self.assertEqual(cli._caller_orch(self.con, "registered"), "registered")

    def test_cli_dispatch_and_report_use_worker_and_owner_ids(self):
        rt = FakeRuntime()

        def start(con, runtime, task, *args, **kwargs):
            store.update_task(con, task["id"], session_id="worker-session", active_turn="worker-turn")
            core.ack(con, task["id"])

        with patch.dict(os.environ, {"CODEX_THREAD_ID": "caller-thread"}), \
             patch("orchd.home_migrations.ensure_current", return_value=[]), \
             patch.object(cli, "Runtime", return_value=rt), \
             patch.object(core, "app_brief", return_value="brief"), \
             patch.object(core.app_worker, "start", side_effect=start):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(cli.main(["dispatch", "--repo", "demo", "--model", "sol",
                                           "--backend", "app-server", "--title", "read",
                                           "--instructions", "read the file", "--done-when", "line reported",
                                           "--model-reason", "read-only", "--task-type", "investigation"]), 0)
            task = json.loads(out.getvalue())
            self.assertEqual((task["orch_id"], task["worker_thread_id"], task["worker_turn_id"], task["status"]),
                             ("caller-thread", "worker-session", "worker-turn", "acked"))
            core.report(self.con, rt, task["task_id"], "done", "read", "the first line")
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(cli.main(["inbox"]), 0)
            report = next(row for row in json.loads(out.getvalue()) if row["kind"] == "report")
            self.assertEqual((report["task_id"], report["evidence"]), (task["task_id"], "the first line"))
            self.assertFalse(store.unread_for_thread(self.con, "caller-thread"))
