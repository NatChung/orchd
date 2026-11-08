import tempfile
import unittest
from pathlib import Path

from orchd import channel_layout


class ChannelLayoutTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.env = {"ORCHD_ROOT": str(self.home / "custom"), "ORCHD_HOME": str(self.home / "state")}

    def test_fresh_apply_and_repeat_preserve_disabled_templates(self):
        first = channel_layout.apply(home=self.home, env=self.env)
        root = self.home / "custom" / "channel"
        self.assertEqual(first["layout_version"], 1)
        before = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}
        self.assertIn(b"outbound_enabled = false", before["channels.toml"])
        for name in ("email", "slack", "line"):
            self.assertIn(b"enabled = false", before[name + "/config.toml"])
            self.assertTrue((root / name / "state").is_dir())
            self.assertIn(name + "/mapping.toml", before)
        channel_layout.apply(home=self.home, env=self.env)
        self.assertEqual(before, {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()})

    def test_plan_does_not_write(self):
        channel_layout.plan(home=self.home, env=self.env)
        self.assertEqual(list(self.home.iterdir()), [])

    def test_existing_conflict_is_not_overwritten(self):
        file = self.home / "custom/channel/slack/config.toml"
        file.parent.mkdir(parents=True)
        file.write_bytes(b"user content")
        with self.assertRaises(ValueError):
            channel_layout.apply(home=self.home, env=self.env)
        self.assertEqual(file.read_bytes(), b"user content")

    def test_rollback_preserves_new_state(self):
        receipt = channel_layout.apply(home=self.home, env=self.env)
        state = self.home / "custom/channel/email/state/spool"
        state.write_bytes(b"new data")
        channel_layout.rollback(receipt["migration_id"], home=self.home, env=self.env, apply=True)
        self.assertEqual(state.read_bytes(), b"new data")


    def test_interrupted_apply_resumes_same_receipt(self):
        from unittest.mock import patch
        real = channel_layout._atomic
        calls = []
        def interrupted(path, data):
            calls.append(path)
            if len(calls) == 2:
                raise OSError("synthetic interruption")
            real(path, data)
        with patch.object(channel_layout, "_atomic", side_effect=interrupted):
            with self.assertRaises(OSError):
                channel_layout.apply(home=self.home, env=self.env)
        result = channel_layout.apply(home=self.home, env=self.env)
        self.assertEqual(result["layout_version"], 1)
        self.assertTrue((self.home / "custom/channel/slack/config.toml").is_file())

    def test_symlink_check_rejects_reparse_point(self):
        from unittest.mock import patch
        target = self.home / "custom/channel"
        target.mkdir(parents=True)
        original = Path.is_symlink
        with patch.object(Path, "is_symlink", lambda p: True if p == target else original(p)):
            with self.assertRaisesRegex(ValueError, "symlink"):
                channel_layout.plan(home=self.home, env=self.env)

    @unittest.skipUnless(__import__("os").name == "nt", "Windows ACL API only")
    def test_windows_acl_is_private(self):
        from orchd import platform_process
        channel_layout.apply(home=self.home, env=self.env)
        for directory in (self.home / "custom/channel", self.home / "custom/channel/email/state"):
            platform_process.validate_private(directory)
        channel_layout._validate_file(self.home / "custom/channel/email/config.toml")

    def test_repeat_does_not_change_journal_bytes(self):
        channel_layout.apply(home=self.home, env=self.env)
        state = self.home / "state/migrations/channels/layout.sqlite3"
        before = state.read_bytes()
        channel_layout.apply(home=self.home, env=self.env)
        self.assertEqual(before, state.read_bytes())

    def test_real_symlink_is_rejected_or_privilege_skip(self):
        target = self.home / "outside"
        target.mkdir()
        link = self.home / "custom/channel"
        link.parent.mkdir()
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"creating Windows symlink unavailable: {error}")
        with self.assertRaisesRegex(ValueError, "symlink"):
            channel_layout.plan(home=self.home, env=self.env)

    def test_secret_bearing_explicit_legacy_manifest_is_not_backed_up(self):
        legacy = self.home / "legacy.toml"
        legacy.write_text('[source]\ntoken = "synthetic-sensitive-value"\n', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "secret_externalization_required"):
            channel_layout.apply(home=self.home, env=self.env, legacy_manifest=legacy)
        self.assertFalse((self.home / "state").exists())
        self.assertFalse((self.home / "custom").exists())

    def test_cli_classifies_layout_operations_without_startup_migration(self):
        from orchd import cli
        parser = cli.build_parser()
        for argv in (["install", "--dry-run", "--json"],
                     ["channels", "layout", "plan", "--legacy-manifest", "synthetic.toml"],
                     ["channels", "layout", "rollback", "--migration", "abc", "--dry-run"]):
            self.assertEqual(cli._home_policy(parser.parse_args(argv)), "skip")

    def test_rollback_keeps_a_user_modified_file(self):
        receipt = channel_layout.apply(home=self.home, env=self.env)
        changed = self.home / "custom/channel/slack/config.toml"
        changed.write_bytes(b"user edit")
        result = channel_layout.rollback(receipt["migration_id"], home=self.home, env=self.env, apply=True)
        self.assertEqual(changed.read_bytes(), b"user edit")
        self.assertIn(str(changed), result["conflicts"])

    def test_explicit_channel_home_is_respected(self):
        env = {**self.env, "ORCHD_CHANNEL_HOME": str(self.home / "elsewhere")}
        channel_layout.apply(home=self.home, env=env)
        self.assertTrue((self.home / "elsewhere/line/config.toml").is_file())
        self.assertFalse((self.home / "custom/channel").exists())

    def test_missing_file_after_completed_migration_is_not_recreated(self):
        channel_layout.apply(home=self.home, env=self.env)
        target = self.home / "custom/channel/slack/config.toml"
        target.unlink()
        with self.assertRaisesRegex(ValueError, "conflict"):
            channel_layout.apply(home=self.home, env=self.env)
        self.assertFalse(target.exists())

    def test_rollback_preview_reports_new_state_without_writing(self):
        receipt = channel_layout.apply(home=self.home, env=self.env)
        state = self.home / "custom/channel/email/state/spool"
        state.write_bytes(b"synthetic")
        before = {str(p): p.read_bytes() for p in self.home.rglob("*") if p.is_file()}
        result = channel_layout.rollback(receipt["migration_id"], home=self.home, env=self.env, apply=False)
        self.assertIn(str(state.parent), result["conflicts"])
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.home.rglob("*") if p.is_file()})

    def test_standalone_layout_uses_registered_home(self):
        from orchd import home_migrations
        root = self.home / "registered"
        state = self.home / "state"
        home_migrations.record_init(self.home, {"ORCHD_ROOT": str(root), "ORCHD_HOME": str(state)})
        env = {"ORCHD_HOME": str(state)}
        channel_layout.apply(home=self.home, env=env)
        self.assertTrue((root / "channel/email/config.toml").is_file())
        self.assertFalse((self.home / "orch/channel").exists())

    def test_insecure_existing_channel_directory_is_rejected_before_state_write(self):
        import os
        root = self.home / "custom/channel"
        root.mkdir(parents=True)
        if os.name != "nt":
            root.chmod(0o755)
        with self.assertRaises(ValueError):
            channel_layout.apply(home=self.home, env=self.env)
        self.assertFalse((self.home / "state").exists())
