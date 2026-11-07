"""Native Windows setup keeps project permissions and paths intact."""
import os
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from orchd import bootstrap, config, entry_attachments, home_migrations


@unittest.skipUnless(os.name == "nt", "native Windows setup")
class WindowsSetupTests(unittest.TestCase):
    def test_init_round_trips_windows_paths_and_keeps_restricted_interface(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / 'Example "Operator"'
            # Quotes are not valid directory names on Windows. Exercise TOML
            # quoting separately while real setup uses a Unicode, spaced path.
            self.assertEqual(tomllib.loads("path = " + bootstrap._toml_string(str(home)))["path"], str(home))
            home = Path(tmp) / "Example 操作員"
            home.mkdir()
            result = bootstrap.init(mock.Mock(), home=home, env={}, trust=False)
            interface = Path(result["interface"])
            data = tomllib.loads((interface / ".codex/config.toml").read_text(encoding="utf-8"))
            self.assertEqual(data["model"], "gpt-6.1-terra")
            self.assertEqual(data["default_permissions"], "interface")
            self.assertEqual(data["windows"]["sandbox"], "elevated")
            self.assertEqual(data["project_doc_max_bytes"], 0)
            self.assertIn("orchd_entry", data["developer_instructions"])
            self.assertIn("ALL_TOOLS", data["developer_instructions"])
            fs = data["permissions"]["interface"]["filesystem"]
            self.assertEqual(fs[str(home)], "deny")
            self.assertEqual(fs[str(interface)], "read")
            self.assertEqual(fs[":minimal"], "read")
            self.assertNotIn(":root", fs)
            self.assertFalse(data["permissions"]["interface"]["network"]["enabled"])
            self.assertFalse(data["features"]["shell_tool"])
            second = bootstrap.init(mock.Mock(), home=home, env={}, trust=False)
            self.assertEqual(set(second["files"].values()), {"unchanged"})

    def test_codex_trust_and_project_setting_round_trip_drive_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            folder = home / "orch/interface"
            file = home / "codex/config.toml"
            bootstrap.codex_trust(file, [folder])
            data = tomllib.loads(file.read_text(encoding="utf-8"))
            self.assertEqual(data["projects"][str(folder.resolve())]["trust_level"], "trusted")
            config.set_value("projects-dir", str(home / "My Projects"), {}, home)
            self.assertEqual(config.file_projects_dir({}, home), str(home / "My Projects"))

    def test_attachment_copy_is_explicitly_unavailable_without_reading_source(self):
        sources = [r"C:\Users\Example\secret.png"]
        with mock.patch.object(entry_attachments, "_copy", side_effect=AssertionError("source read")):
            self.assertEqual(entry_attachments.capture("entry", 1, sources), "unsupported_platform")
            items, omitted = entry_attachments.describe("entry", 1, sources)
        self.assertEqual(omitted, 0)
        self.assertEqual(items, [dict(source_path=sources[0], status="unavailable", error="unsupported_platform")])

    def test_init_refreshes_managed_rules_preserving_custom_model_and_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            bootstrap.init(mock.Mock(), home=home, env={}, trust=False)
            interface = home / "orch/interface"
            file = interface / ".codex/config.toml"
            text = file.read_text(encoding="utf-8").replace('model = "gpt-6.1-terra"', 'model = "example-custom-model"')
            file.write_text(text, encoding="utf-8")
            old = tomllib.loads(text)
            rules = interface / "AGENTS.md"
            rules.write_text(rules.read_text(encoding="utf-8") + "\nLocal Operator instructions.\n", encoding="utf-8")
            bootstrap.init(mock.Mock(), home=home, env={}, trust=False)
            new = tomllib.loads(file.read_text(encoding="utf-8"))
            self.assertIn("Local Operator instructions.", new["developer_instructions"])
            self.assertEqual(new, dict(old, developer_instructions=bootstrap.interface_instructions(interface)))
            backups = home_migrations.state_dir(home, {}) / "backups/interface-config"
            self.assertTrue(any(backups.iterdir()))

    def test_upgrade_syncs_the_current_interface_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            bootstrap.init(mock.Mock(), home=home, env={}, trust=False)
            interface = home / "orch/interface"
            rules = interface / "AGENTS.md"
            rules.write_text("Operator prefix.\n" + rules.read_text(encoding="utf-8"), encoding="utf-8")
            home_migrations.update_rules({"interface": interface}, False, [], home / "backups")
            config_data = tomllib.loads((interface / ".codex/config.toml").read_text(encoding="utf-8"))
            self.assertTrue(config_data["developer_instructions"].startswith("Operator prefix.\n"))


class ManagedRulesLineEndingsTests(unittest.TestCase):
    def test_crlf_template_updates_idempotently_and_preserves_outside_bytes(self):
        template = b"<!-- orchd:managed:start v=1 -->\r\nnew\r\n<!-- orchd:managed:end -->\r\n"
        old = b"before\r\n<!-- orchd:managed:start v=0 -->\nold\n<!-- orchd:managed:end -->\r\nafter"
        updated, _ = home_migrations.updated(old, template)
        again, _ = home_migrations.updated(updated, template)
        self.assertEqual(again, updated)
        self.assertTrue(updated.startswith(b"before\r\n"))
        self.assertTrue(updated.endswith(b"\r\nafter"))

