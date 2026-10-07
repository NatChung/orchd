import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchd.runtime import Runtime, ORCH_TOOLS


class OrchPermissionsTest(unittest.TestCase):
    def test_only_groups_are_writable_and_only_this_homes_temp_tree_is_added(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve() / "orch home"
            home.mkdir()
            (home / "AGENTS.md").write_text("Keep group notes.")
            rt = Runtime()
            sock = str(Path(tmp) / "o.sock")
            with mock.patch.object(rt, "orch_socket_path", return_value=sock), \
                    mock.patch.object(rt, "start_claude", return_value=("job", "session")) as start, \
                    mock.patch.object(Path, "mkdir") as mkdir, \
                    mock.patch.object(Path, "chmod") as chmod:
                self.assertEqual(rt.start_orch("o38", "opus", home), (sock, "job", "session"))
            cwd, actual_sock, model, args = start.call_args.args
            self.assertEqual((cwd, actual_sock, model), (home, sock, "opus"))
            self.assertNotIn("--session-id", args)
            self.assertNotIn("--restricted", args)
            self.assertEqual(args[args.index("--tools") + 1], ORCH_TOOLS)
            self.assertEqual(args[args.index("--setting-sources") + 1], "")
            self.assertNotIn("--disable-slash-commands", args)
            for tool in ("Read", "Grep", "Glob", "Edit", "Write", "Skill", "ToolSearch", "Artifact"):
                self.assertIn(tool, ORCH_TOOLS.split(","))
            for tool in ("Bash", "PowerShell", "REPL", "WebFetch"):
                self.assertNotIn(tool, ORCH_TOOLS.split(","))
            self.assertIn("--no-chrome", args)
            self.assertEqual(args[args.index("--permission-mode") + 1], "dontAsk")
            self.assertIn("--strict-mcp-config", args)
            settings = json.loads(args[args.index("--settings") + 1])
            self.assertEqual(settings["worktree"], {"bgIsolation": "none"})
            self.assertEqual(settings["crossSessionInbound"], "accept")
            self.assertEqual(settings["permissions"]["disableBypassPermissionsMode"], "disable")
            self.assertEqual(settings["env"]["CLAUDE_CODE_DISABLE_AUTO_MEMORY"], "1")
            mcp = json.loads(Path(args[args.index("--mcp-config") + 1]).read_text())
            self.assertEqual(set(mcp["mcpServers"]), {"orchd"})
            self.assertEqual(settings["permissions"]["allow"],
                             [f"Read(/{home}/**)", f"Read(/{settings['permissions']['additionalDirectories'][0]}/**)",
                              f"Edit(/{home}/groups/**)", "mcp__orchd"])
            self.assertTrue(settings["permissions"]["blockReadsOutsideWorkingDirectories"])
            self.assertIn(f"Edit(/{home}/CLAUDE.md)", settings["permissions"]["deny"])
            for pattern in (".*", ".*/**", "AGENTS.md", "CLAUDE.md", "settings.json",
                            "settings.local.json", "mcp.json"):
                self.assertIn(f"Edit(/{home}/groups/**/{pattern})", settings["permissions"]["deny"])
            images, = settings["permissions"]["additionalDirectories"]
            key = re.sub(r"[^a-zA-Z0-9]", "-", str(home))
            self.assertEqual(images, str((Path("/tmp") / f"claude-{os.getuid()}" / key).resolve()))
            mkdir.assert_called_once_with(mode=0o700, parents=True, exist_ok=True)
            chmod.assert_called_once_with(0o700)
            self.assertNotIn("Edit", settings["permissions"]["allow"])
            self.assertNotIn("Write", settings["permissions"]["allow"])
            self.assertIn(images, args[args.index("--append-system-prompt") + 1])
            self.assertIn("Keep group notes.", args[args.index("--append-system-prompt") + 1])

    def test_long_home_fails_closed_before_starting_a_session(self):
        rt = Runtime()
        with mock.patch.object(rt, "start_claude") as start:
            with self.assertRaisesRegex(RuntimeError, "image-directory key limit"):
                rt.start_orch("o38", "opus", "/tmp/" + "x" * 201)
        start.assert_not_called()
