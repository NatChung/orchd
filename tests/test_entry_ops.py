"""Interface-requested Orch restart / orchd upgrade: temporary state, fake commands, no real processes."""
import json
import os
import tempfile
import time
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from orchd import bootstrap, entry_ops, mcp_server, store
from orchd.file_lock import try_lock

RESTART_DONE = {"status": "done", "step": "done", "old_orch": "oold0001", "new_orch": "onew0001",
                "adopt": {"adopted": ["aaaa1111", "bbbb2222"]}}
RESTART_NOTICE = {"status": "failed", "step": "binding-status", "old_orch": "oold0001", "new_orch": "onew0001",
                  "new_health": {"state": "alive"}, "binding_status": {"orch_id": "onew0001"},
                  "adopt": {"adopted": []},
                  "error": "ValueError: binding committed but the new Orch's binding notice failed",
                  "manual_recovery": "orchd binding --status"}
RESTART_STOPPED = {"status": "failed", "step": "confirm-dead", "old_orch": "oold0001", "new_orch": None,
                   "error": "ValueError: old Orch is not confirmed dead", "manual_recovery": "orchd orch list --all"}


class Commands:
    """Records each orchd invocation and answers from a script."""

    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def __call__(self, argv):
        self.calls.append(argv[-2:] if argv[-2:] == ["orch", "restart"] else argv[-1:])
        return self.answers.pop(0)


def restart_out(result):
    return 0 if result["status"] == "done" else 1, json.dumps(result) + "\nNew Orch: x\n", ""


class EntryOpsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.dict(os.environ, {"ORCHD_HOME": self.tmp.name, "ORCHD_EXECUTABLE": "orchd-test"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.spawned = []

    def spawn(self, argv, log):
        self.spawned.append(argv)
        return SimpleNamespace(pid=4242)

    def start(self, kind="restart", **kw):
        return entry_ops.start(kind, entry_id="desktop", thread="desk-1", spawn=self.spawn, **kw)

    def test_start_records_and_launches_the_detached_runner(self):
        out = self.start()
        self.assertEqual(out["state"], "queued")
        self.assertEqual(self.spawned, [["orchd-test", "_entry-op", out["op_id"]]])
        saved = entry_ops.read(out["op_id"])
        self.assertEqual((saved["kind"], saved["runner_pid"], saved["requested_by_thread"]), ("restart", 4242, "desk-1"))
        self.assertNotIn("log", out)

    def test_start_is_refused_while_another_operation_holds_the_lock(self):
        first = self.start()
        with entry_ops._lock_file() as handle:
            try_lock(handle)
            with self.assertRaisesRegex(ValueError, f"still running.*{first['op_id']}"):
                self.start("upgrade")
        self.assertEqual(len(self.spawned), 1)

    def test_restart_done(self):
        op = self.start()["op_id"]
        commands = Commands(restart_out(RESTART_DONE))
        self.assertEqual(entry_ops.run(op, run_command=commands), 0)
        self.assertEqual(commands.calls, [["orch", "restart"]])
        out = entry_ops.public(entry_ops.read(op))
        self.assertEqual(out["state"], "done")
        self.assertEqual((out["old_orch"], out["new_orch"], out["adopted_tasks"]),
                         ("oold0001", "onew0001", ["aaaa1111", "bbbb2222"]))
        self.assertIn("moved 2 open task(s)", out["summary"])
        self.assertIsNotNone(out["finished_at"])

    def test_missed_binding_notice_alone_is_a_warning(self):
        op = self.start()["op_id"]
        self.assertEqual(entry_ops.run(op, run_command=Commands(restart_out(RESTART_NOTICE))), 0)
        out = entry_ops.public(entry_ops.read(op))
        self.assertEqual(out["state"], "done_with_warnings")
        self.assertIn("bound to onew0001", out["summary"])

    def test_restart_failure_carries_error_and_recovery(self):
        op = self.start()["op_id"]
        self.assertEqual(entry_ops.run(op, run_command=Commands(restart_out(RESTART_STOPPED))), 1)
        out = entry_ops.public(entry_ops.read(op))
        self.assertEqual(out["state"], "failed")
        self.assertEqual(out["manual_recovery"], "orchd orch list --all")
        self.assertIn("confirm-dead", out["summary"])

    def test_unparseable_restart_output_fails(self):
        op = self.start()["op_id"]
        self.assertEqual(entry_ops.run(op, run_command=Commands((1, "", "boom"))), 1)
        self.assertEqual(entry_ops.read(op)["state"], "failed")

    def test_upgrade_then_restart_on_new_code(self):
        op = self.start("upgrade")["op_id"]
        commands = Commands((0, "orchd abc1234 -> def5678\nWindows upgrade activated\n", ""), restart_out(RESTART_DONE))
        self.assertEqual(entry_ops.run(op, run_command=commands), 0)
        self.assertEqual(commands.calls, [["upgrade"], ["orch", "restart"]])
        out = entry_ops.public(entry_ops.read(op))
        self.assertEqual(out["state"], "done")
        self.assertTrue(out["summary"].startswith("orchd upgraded."))
        self.assertIn("orchd abc1234 -> def5678", out["upgrade_lines"])
        self.assertIn("new conversation", out["note"])
        self.assertEqual([s["step"] for s in out["steps"]], ["upgrade", "restart"])

    def test_failed_upgrade_never_restarts(self):
        op = self.start("upgrade")["op_id"]
        commands = Commands((1, "", "staged upgrade failed; the current environment remains active:"))
        self.assertEqual(entry_ops.run(op, run_command=commands), 1)
        self.assertEqual(commands.calls, [["upgrade"]])
        self.assertEqual(entry_ops.read(op)["state"], "failed")

    def test_up_to_date_or_restart_false_skips_restart(self):
        op = self.start("upgrade")["op_id"]
        commands = Commands((0, "orchd is already the newest selected commit (abc1234).\n", ""))
        self.assertEqual(entry_ops.run(op, run_command=commands), 0)
        self.assertEqual(commands.calls, [["upgrade"]])
        self.assertIn("already up to date", entry_ops.read(op)["summary"])
        op = self.start("upgrade", restart=False)["op_id"]
        commands = Commands((0, "orchd abc1234 -> def5678\n", ""))
        self.assertEqual(entry_ops.run(op, run_command=commands), 0)
        self.assertEqual(commands.calls, [["upgrade"]])
        self.assertIn("restart=false", entry_ops.read(op)["summary"])

    def test_crash_is_recorded_and_runner_that_vanished_reads_as_lost(self):
        op = self.start()["op_id"]

        def crash(argv):
            raise RuntimeError("kaboom")
        self.assertEqual(entry_ops.run(op, run_command=crash), 1)
        self.assertEqual(entry_ops.read(op)["state"], "failed")
        record = dict(entry_ops.read(op), state="running", started_at=time.time())
        self.assertEqual(entry_ops.public(record)["state"], "lost")
        fresh = dict(record, started_at=None, requested_at=time.time())
        self.assertEqual(entry_ops.public(fresh)["state"], "running")

    def test_op_ids_are_validated(self):
        for bad in ("../x", "ABC", None):
            with self.assertRaises(ValueError):
                entry_ops.read(bad)
        self.assertEqual(entry_ops.public(None)["state"], "none")


class EntryToolTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.dict(os.environ, {"ORCHD_HOME": self.tmp.name, "ORCHD_EXECUTABLE": "orchd-test"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.con = store.connect(Path(self.tmp.name) / "state.db")
        self.addCleanup(self.con.close)

    def call(self, name, args=None, entry_id="desktop"):
        return mcp_server.handle({"id": 1, "method": "tools/call", "params": {
            "name": name, "arguments": args or {}, "_meta": {"threadId": "desk-1"}}},
            self.con, None, role="entry", entry_id=entry_id)["result"]

    def test_tools_are_listed_and_instructions_limit_them_to_explicit_requests(self):
        listed = mcp_server.handle({"id": 1, "method": "tools/list"}, self.con, None, role="entry")["result"]["tools"]
        self.assertLessEqual({"orch_restart", "orch_upgrade", "orch_op_status"}, {t["name"] for t in listed})
        init = mcp_server.handle({"id": 1, "method": "initialize", "params": {}}, self.con, None, role="entry")
        self.assertIn("Only when Operator explicitly asks", init["result"]["instructions"])
        self.assertNotIn("orch_restart", {t["name"] for t in mcp_server.TOOLS})

    def test_restart_upgrade_and_status_through_the_entry(self):
        self.assertEqual(json.loads(self.call("orch_op_status")["content"][0]["text"])["state"], "none")
        launched = []
        with patch.object(entry_ops, "_spawn", lambda argv, log: launched.append(argv) or SimpleNamespace(pid=1)):
            started = self.call("orch_upgrade", {"restart": False})
        self.assertNotIn("isError", started)
        op = started["structuredContent"]
        self.assertEqual((op["kind"], op["restart"], op["state"]), ("upgrade", False, "queued"))
        self.assertEqual(launched, [["orchd-test", "_entry-op", op["op_id"]]])
        status = self.call("orch_op_status", {"op_id": op["op_id"]})["structuredContent"]
        self.assertEqual(status["op_id"], op["op_id"])

    def test_refusals(self):
        self.assertTrue(self.call("orch_restart", {"old_id": "o123"})["isError"])
        self.assertTrue(self.call("orch_upgrade", {"restart": "yes"})["isError"])
        refused = self.call("orch_restart", entry_id="other")
        self.assertIn("only to the Desktop entry", refused["content"][0]["text"])


class InterfaceConfigMigrationTest(unittest.TestCase):
    OLD = ('project_doc_max_bytes = 0\n'
           'developer_instructions = "<!-- orchd:managed:start v=1 -->\\n# interface\\nold rules"\n\n'
           'model = "gpt-6.1-terra"\n\n'
           '[mcp_servers.orchd_entry]\ncommand = "orchd.exe"\nargs = ["mcp", "--role", "entry"]\n\n'
           '[mcp_servers.orchd_entry.tools.relay]\napproval_mode = "approve"\n\n'
           '[mcp_servers.orchd_entry.tools.status]\napproval_mode = "approve"\n\n'
           '[mcp_servers.orchd_entry.tools.foreground]\napproval_mode = "approve"\n\n'
           '[windows]\nsandbox = "elevated"\n')

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.interface = Path(self.tmp.name) / "interface"
        (self.interface / ".codex").mkdir(parents=True)
        self.file = self.interface / ".codex" / "config.toml"
        self.backups = Path(self.tmp.name) / "backups"

    def sync(self, **kw):
        return bootstrap.sync_interface_entry_tools(self.interface, self.backups, **kw)

    def test_new_tools_are_appended_and_nothing_else_changes(self):
        self.file.write_text(self.OLD, encoding="utf-8")
        with patch.object(bootstrap, "os", SimpleNamespace(name="posix")):
            self.assertIn("would update", self.sync(dry_run=True)[0])
            self.assertEqual(self.file.read_text(encoding="utf-8"), self.OLD)
            lines = self.sync()
        self.assertIn("orch_restart, orch_upgrade, orch_op_status", lines[0])
        new = tomllib.loads(self.file.read_text(encoding="utf-8"))
        old = tomllib.loads(self.OLD)
        self.assertEqual(set(new["mcp_servers"]["orchd_entry"]["tools"]),
                         {t["name"] for t in mcp_server.ENTRY_TOOLS})
        self.assertEqual(new["windows"], old["windows"])
        self.assertEqual(new["developer_instructions"], old["developer_instructions"])
        self.assertEqual(len(list(self.backups.iterdir())), 1)
        with patch.object(bootstrap, "os", SimpleNamespace(name="posix")):
            self.assertEqual(self.sync(), [f"unchanged: {self.file}"])

    def test_windows_refreshes_orchd_generated_instructions_and_marks_them(self):
        self.file.write_text(self.OLD, encoding="utf-8")
        (self.interface / "AGENTS.md").write_text("<!-- orchd:managed:start v=1 -->\nnew rules orch_restart\n",
                                                  encoding="utf-8")
        with patch.object(bootstrap, "os", SimpleNamespace(name="nt")):
            lines = self.sync()
        text = self.file.read_text(encoding="utf-8")
        self.assertIn("interface instructions refreshed", lines[0])
        self.assertIn("# orchd:interface-instructions\ndeveloper_instructions = ", text)
        self.assertIn("new rules orch_restart", tomllib.loads(text)["developer_instructions"])

    def test_hand_written_instructions_and_user_configs_are_left_alone(self):
        self.file.write_text(self.OLD.replace("<!-- orchd:managed:start v=1 -->", "my own"), encoding="utf-8")
        with patch.object(bootstrap, "os", SimpleNamespace(name="nt")):
            lines = self.sync()
        self.assertNotIn("instructions refreshed", lines[0])
        self.assertEqual(tomllib.loads(self.file.read_text(encoding="utf-8"))["developer_instructions"],
                         "my own\n# interface\nold rules")
        self.file.write_text('model = "x"\n', encoding="utf-8")
        self.assertIn("user-managed", self.sync()[0])
        self.file.unlink()
        self.assertEqual(self.sync(), [])


if __name__ == "__main__":
    unittest.main()
