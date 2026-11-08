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
        before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}
        self.assertIn(b"outbound_enabled = false", before["channels.toml"])
        for name in ("email", "slack", "line"):
            self.assertIn(b"enabled = false", before[name + "/config.toml"])
            self.assertTrue((root / name / "state").is_dir())
            self.assertIn(name + "/mapping.toml", before)
        channel_layout.apply(home=self.home, env=self.env)
        self.assertEqual(before, {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()})

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
