import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchd import bootstrap, home_migrations as migrations


class HomeMigrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.env = {"ORCHD_HOME": str(self.home / "state")}
        self.file = self.home / "orch/home/AGENTS.md"

    def run_migrations(self, **kw):
        return migrations.run(home=self.home, env=self.env, **kw)

    def backups(self, role):
        return self.home / "state/backups" / role

    def existing(self, text):
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self.file.write_bytes(text)

    def applied(self):
        con = migrations.connect(self.home, self.env, readonly=True)
        if con is None:
            return []
        try:
            return con.execute("SELECT step FROM applied").fetchall()
        finally:
            con.close()

    def test_first_append_preserves_original_and_backup_and_retry_is_noop(self):
        original = "# 自訂規則\r\n原文不要改".encode()
        self.existing(original)
        self.file.chmod(0o640)
        code, lines = self.run_migrations()
        self.assertEqual(code, 0, lines)
        content = self.file.read_bytes()
        self.assertTrue(content.startswith(original))
        self.assertEqual(content.count(b"orchd:managed:start"), 1)
        backups = list(self.backups("home").glob("AGENTS.md.*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)
        self.assertEqual(backups[0].parent, (self.home / "state/backups/home").absolute())
        self.assertEqual(list(self.file.parent.glob("*orchd-backup*")), [])
        self.assertEqual(self.file.stat().st_mode & 0o777, 0o640)
        self.assertIn("原檔已有的相同規則可以手動刪掉", "\n".join(lines))
        self.assertIn(str(backups[0]), "\n".join(lines))
        self.assertIn("restore:", "\n".join(lines))
        self.assertEqual(self.applied(), [("orch-rules-v1",)])
        self.assertEqual(self.run_migrations()[0], 0)
        self.assertEqual(self.file.read_bytes(), content)
        self.assertEqual(list(self.backups("home").glob("AGENTS.md.*")), backups)

    def test_block_update_keeps_outside_bytes_and_future_step_order(self):
        prefix, suffix = b"local before\r\n", b"\r\nlocal after without newline"
        self.existing(prefix + b"<!-- orchd:managed:start v=0 -->\nold rules\n<!-- orchd:managed:end -->" + suffix)
        code, lines = self.run_migrations()
        self.assertEqual(code, 0, lines)
        template = (bootstrap.TEMPLATES / "home/AGENTS.md").read_bytes().rstrip(b"\n")
        self.assertEqual(self.file.read_bytes(), prefix + template + suffix)
        # A new release adds a new step; old steps stay recorded and are skipped.
        with patch.object(migrations, "STEPS", (("orch-rules-v1", migrations.update_rules), ("orch-rules-v2", migrations.update_rules))):
            self.assertEqual(self.run_migrations()[0], 0)
        self.assertEqual(self.applied(), [("orch-rules-v1",), ("orch-rules-v2",)])

    def test_missing_files_are_created_from_init_templates(self):
        self.assertEqual(self.run_migrations()[0], 0)
        for role in ("home", "interface"):
            self.assertEqual((self.home / "orch" / role / "AGENTS.md").read_bytes(),
                             (bootstrap.TEMPLATES / role / "AGENTS.md").read_bytes())

    def test_dry_run_has_diff_and_creates_no_state_backups_or_folders(self):
        self.existing(b"local")
        before = sorted(self.home.rglob("*"))
        code, lines = self.run_migrations(dry_run=True)
        self.assertEqual(code, 0, lines)
        self.assertIn("+<!-- orchd:managed:start v=1 -->", "\n".join(lines))
        self.assertEqual(self.file.read_bytes(), b"local")
        self.assertEqual(sorted(self.home.rglob("*")), before)

    def test_malformed_blocks_fail_without_marking_step(self):
        for text in (b"<!-- orchd:managed:start v=1 -->unfinished",
                     b"<!-- orchd:managed:end --><!-- orchd:managed:start v=1 -->",
                     b"<!-- orchd:managed:start v=1 --><!-- orchd:managed:end -->" * 2):
            with self.subTest(text=text):
                self.existing(text)
                code, lines = self.run_migrations()
                self.assertEqual(code, 1)
                self.assertEqual(self.file.read_bytes(), text)
                self.assertEqual(self.applied(), [])
                self.assertIn("--migrations-only", "\n".join(lines))

    def test_partial_failure_retries_without_duplicate_append(self):
        self.existing(b"local")
        real_write = migrations.write_file
        def fail_interface(file, content, backup_dir):
            if file.parent.name == "interface":
                raise OSError("disk full")
            return real_write(file, content, backup_dir)
        with patch.object(migrations, "write_file", side_effect=fail_interface):
            code, lines = self.run_migrations()
        self.assertEqual(code, 1)
        self.assertEqual(self.applied(), [])
        self.assertIn("backup:", "\n".join(lines))
        self.assertEqual(self.run_migrations()[0], 0)
        self.assertEqual(self.file.read_bytes().count(b"orchd:managed:start"), 1)
        self.assertEqual(len(list(self.backups("home").glob("AGENTS.md.*"))), 1)
        self.assertEqual(list(self.home.rglob("*orchd-backup-*")), [])

    def test_failed_atomic_write_preserves_original_and_reports_backup(self):
        self.existing(b"local")
        with patch.object(migrations.os, "replace", side_effect=OSError("disk full")):
            code, lines = self.run_migrations()
        self.assertEqual(code, 1)
        self.assertEqual(self.file.read_bytes(), b"local")
        self.assertEqual(self.applied(), [])
        backup = next(self.backups("home").glob("AGENTS.md.*"))
        self.assertEqual(backup.read_bytes(), b"local")
        self.assertIn(str(backup), "\n".join(lines))

    def test_ordered_steps_keep_successful_predecessors_on_failure(self):
        def fail(folders, dry_run, lines, backup_root):
            raise OSError("step two failed")
        with patch.object(migrations, "STEPS", (("one", migrations.update_rules), ("two", fail))):
            self.assertEqual(self.run_migrations()[0], 1)
        self.assertEqual(self.applied(), [("one",)])
        with patch.object(migrations, "STEPS", (("one", fail), ("two", migrations.update_rules))):
            code, lines = self.run_migrations()
        self.assertEqual(code, 0, lines)  # step one is skipped, not rerun
        self.assertEqual(self.applied(), [("one",), ("two",)])

    def test_symlink_is_refused_without_modifying_target(self):
        target = self.home / "other.md"
        target.write_bytes(b"local")
        self.file.parent.mkdir(parents=True)
        self.file.symlink_to(target)
        self.assertEqual(self.run_migrations()[0], 1)
        self.assertEqual(target.read_bytes(), b"local")
        self.assertEqual(self.applied(), [])

    def test_init_record_and_explicit_overrides_control_scope(self):
        custom = dict(self.env, ORCHD_ORCH_HOME=str(self.home / "custom-orch"),
                      ORCHD_INTERFACE_HOME=str(self.home / "custom-interface"))
        bootstrap.init(None, home=self.home, env=custom, trust=False, orchd_bin="/opt/orchd")
        self.assertEqual(self.run_migrations()[0], 0)
        self.assertFalse((self.home / "orch").exists())
        overridden = dict(self.env, ORCHD_ROOT=str(self.home / "other"))
        self.assertEqual(migrations.run(home=self.home, env=overridden)[0], 0)
        self.assertTrue((self.home / "other/home/AGENTS.md").is_file())
        self.assertEqual(len(self.applied()), 2)  # step completion is scoped to home paths

    def test_cli_dry_run_and_migrations_only_need_no_uv_install(self):
        env = dict(os.environ, **self.env, ORCHD_ROOT=str(self.home / "orch"))
        env.pop("ORCHD_ORCH_HOME", None)
        env.pop("ORCHD_INTERFACE_HOME", None)
        def cli(*flags):
            return subprocess.run([sys.executable, "bin/orchd", "upgrade", *flags],
                                  env=env, capture_output=True, text=True)
        preview = cli("--dry-run")
        self.assertEqual(preview.returncode, 0, preview.stderr)
        self.assertFalse(self.file.exists())
        applied = cli("--migrations-only")
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertTrue(self.file.exists())
        self.assertIn("already applied", cli("--migrations-only").stdout)

    def test_roles_do_not_overwrite_each_other_and_no_backup_beside_original(self):
        self.existing(b"home")
        interface = self.home / "orch/interface/AGENTS.md"
        interface.parent.mkdir(parents=True)
        interface.write_bytes(b"interface")
        self.assertEqual(self.run_migrations()[0], 0)
        self.assertEqual(next(self.backups("home").glob("AGENTS.md.*")).read_bytes(), b"home")
        self.assertEqual(next(self.backups("interface").glob("AGENTS.md.*")).read_bytes(), b"interface")
        for folder in (self.file.parent, interface.parent):
            self.assertEqual([p.name for p in folder.iterdir() if "orchd-backup" in p.name], [])
