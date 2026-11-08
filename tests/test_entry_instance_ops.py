"""Instance entry operations use temporary profiles and fake detached runners."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from orchd import entry_ops, instances, mcp_server, store, workflows


COMMIT = "b" * 40
CURRENT = "sha256:" + "a" * 64


class EntryInstanceOpsTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / "source"
        self.profile = self.root / "instance"
        self.projects = self.root / "projects"
        self.projects.mkdir()
        env = dict(ORCHD_ROOT=str(self.profile), ORCHD_HOME=str(self.profile / "state"),
                   ORCHD_CONFIG_DIR=str(self.profile / "config"), ORCHD_PROJECTS=str(self.projects),
                   ORCHD_EXECUTABLE="orchd-test")
        patched = patch.dict(os.environ, env)
        patched.start()
        self.addCleanup(patched.stop)
        self.receipt = dict(source_repo=str(self.source), source_commit="a" * 40, profile=env)
        self.real_reader = instances.read_receipt
        reader = patch.object(instances, "read_receipt", return_value=(self.receipt, CURRENT))
        self.reader = reader.start()
        self.addCleanup(reader.stop)
        self.spawned = []
        spawn = patch.object(entry_ops, "_spawn", self.spawn)
        spawn.start()
        self.addCleanup(spawn.stop)
        self.con = store.connect(self.root / "test.db")
        self.addCleanup(self.con.close)

    def spawn(self, argv, log):
        self.spawned.append(argv)
        return SimpleNamespace(pid=42)

    def call(self, name, args=None, role="entry", entry_id="desktop"):
        return mcp_server.handle({"id": 1, "method": "tools/call", "params": {
            "name": name, "arguments": args or {}, "_meta": {"threadId": "desktop-thread"}}},
            self.con, None, role=role, entry_id=entry_id)["result"]

    def test_check_persists_fixed_source_and_returns_preview_through_status(self):
        out = self.call("instance_check", {"commit": COMMIT})["structuredContent"]
        self.assertEqual(out["state"], "queued")
        self.assertFalse(out["restart"])
        self.assertEqual(self.spawned, [["orchd-test", "_entry-op", out["op_id"]]])
        saved = entry_ops.read(out["op_id"])
        self.assertEqual(saved["requested_by_thread"], "desktop-thread")
        self.assertEqual(saved["instance"]["source_repo"], str(self.source))
        result = dict(status="instance_checked", source_commit=COMMIT, changed_files=["home/PROJECTS.md"],
                      current_receipt_digest=CURRENT, restart_required=["orch"])
        with patch.object(instances, "apply", return_value=result) as apply:
            self.assertEqual(entry_ops.run(out["op_id"]), 0)
        apply.assert_called_once_with(str(self.source), COMMIT, str(self.profile), str(self.projects),
                                      check=True, expected_receipt=CURRENT)
        status = self.call("orch_op_status", {"op_id": out["op_id"]})["structuredContent"]
        self.assertEqual(status["instance_result"], result)
        self.assertEqual(status["state"], "done")
        self.assertIsNotNone(status["finished_at"])

    def test_apply_passes_receipt_cas_and_never_restarts(self):
        out = self.call("instance_apply", {"commit": COMMIT, "expected_receipt": CURRENT})["structuredContent"]
        result = dict(status="instance_applied", source_commit=COMMIT, changed_files=["interface/AGENTS.md"],
                      restart_required=["interface"])
        with patch.object(instances, "apply", return_value=result) as apply:
            commands = []
            self.assertEqual(entry_ops.run(out["op_id"], run_command=lambda argv: commands.append(argv)), 0)
        apply.assert_called_once_with(str(self.source), COMMIT, str(self.profile), str(self.projects),
                                      check=False, expected_receipt=CURRENT)
        self.assertEqual(commands, [])
        self.assertEqual(entry_ops.read(out["op_id"])["instance_result"]["restart_required"], ["interface"])

    def test_launch_holds_operation_lock_until_runner_metadata_is_saved(self):
        def launch(argv, log):
            self.assertTrue(entry_ops._busy())
            return self.spawn(argv, log)

        with patch.object(entry_ops, "_spawn", launch):
            out = self.call("instance_check", {"commit": COMMIT})["structuredContent"]
        self.assertFalse(entry_ops._busy())
        self.assertEqual(entry_ops.read(out["op_id"])["runner_pid"], 42)

    def test_stale_receipt_is_rejected_before_enqueue_and_again_by_runner(self):
        stale = "sha256:" + "c" * 64
        self.assertTrue(self.call("instance_apply", {"commit": COMMIT, "expected_receipt": stale})["isError"])
        self.assertEqual(self.spawned, [])
        out = self.call("instance_apply", {"commit": COMMIT, "expected_receipt": CURRENT})["structuredContent"]
        self.reader.return_value = self.receipt, stale
        with patch.object(instances, "apply") as apply:
            self.assertEqual(entry_ops.run(out["op_id"]), 1)
        apply.assert_not_called()
        self.assertEqual(entry_ops.read(out["op_id"])["state"], "failed")

    def test_active_profile_must_match_receipt(self):
        self.receipt["profile"] = dict(self.receipt["profile"], ORCHD_CONFIG_DIR=str(self.root / "elsewhere"))
        self.assertTrue(self.call("instance_check", {"commit": COMMIT})["isError"])
        self.assertEqual(self.spawned, [])

    def test_only_desktop_entry_can_use_instance_tools(self):
        for role, entry_id in (("orch", "desktop"), ("entry", "external-adapter"), ("worker", "desktop")):
            with self.subTest(role=role, entry_id=entry_id):
                result = self.call("instance_check", {"commit": COMMIT}, role=role, entry_id=entry_id)
                self.assertTrue(result["isError"])
        self.assertFalse({"instance_check", "instance_apply"} & {tool["name"] for tool in mcp_server.TOOLS})
        self.assertEqual(self.spawned, [])

    def test_paths_commands_and_approval_flags_are_rejected(self):
        for extra in ("source_repo", "root", "projects", "executable", "command", "approved", "restart", "token"):
            with self.subTest(extra=extra):
                result = self.call("instance_apply", dict(commit=COMMIT, expected_receipt=CURRENT, **{extra: "x"}))
                self.assertTrue(result["isError"])
        for commit in ("main", "a" * 7, COMMIT.upper(), None):
            self.assertTrue(self.call("instance_check", {"commit": commit})["isError"])
        self.assertEqual(self.spawned, [])

    def test_real_committed_update_checks_then_applies_without_starting_sessions(self):
        # The real receipt reader is required for this end-to-end fixture.
        with patch.object(instances, "read_receipt", self.real_reader):
            self.source.mkdir()

            def git(*args):
                return subprocess.check_output(["git", "-C", str(self.source), *args], stderr=subprocess.PIPE).decode().strip()

            def commit():
                git("add", "-A")
                git("-c", "user.name=Example Maintainer", "-c", "user.email=maintainer@example.invalid",
                    "commit", "-qm", "Synthetic instance configuration")
                return git("rev-parse", "HEAD")

            git("init", "-q")
            workflow = dict(schema_version="orchd-workflow/v1", name="example", description="Synthetic workflow.", stages={
                "investigate": dict(task_type="investigation", inputs=[], instructions_file="instructions.md",
                                    done_when_file="criteria.md")})
            manifest = dict(schema_version="orchd-instance/v1", name="example", workflow="example",
                            agents_file="agents.md", projects_file="projects.md")
            for name, value in (("orchd-workflow.json", workflow), ("orchd-instance.json", manifest)):
                (self.source / name).write_bytes(workflows.canonical(value))
            for name, text in (("instructions.md", "Read the question."), ("criteria.md", "Report evidence."),
                               ("agents.md", "Use the approved workflow."), ("projects.md", "Example projects v1.")):
                (self.source / name).write_text(text)
            first = commit()
            # Initial provisioning is a trusted local operation, outside the entry tools.
            instances.apply(self.source, first, self.profile, self.projects)
            (self.source / "projects.md").write_text("Example projects v2.")
            second = commit()
            preview = self.call("instance_check", {"commit": second})["structuredContent"]
            self.assertEqual(entry_ops.run(preview["op_id"]), 0)
            checked = entry_ops.public(entry_ops.read(preview["op_id"]))
            self.assertEqual((self.profile / "home/PROJECTS.md").read_text(), "Example projects v1.")
            self.assertIn("home/PROJECTS.md", checked["instance_result"]["changed_files"])
            request = dict(commit=second, expected_receipt=checked["instance"]["expected_receipt"])
            applied = self.call("instance_apply", request)["structuredContent"]
            self.assertEqual(entry_ops.run(applied["op_id"]), 0)
            self.assertEqual((self.profile / "home/PROJECTS.md").read_text(), "Example projects v2.")
            self.assertTrue(self.call("instance_apply", request)["isError"])
            self.assertFalse((self.profile / "state/orchd.db").exists())


if __name__ == "__main__":
    unittest.main()
