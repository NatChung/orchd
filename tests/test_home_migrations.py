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
        self.assertEqual(self.applied(), [("orch-rules-v1",), ("orch-rules-v2",)])
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

    def test_v2_step_refreshes_a_v1_block_with_the_intake_rule_and_keeps_outside_bytes(self):
        prefix, suffix = b"mine before\n", b"\nmine after"
        self.existing(prefix + b"<!-- orchd:managed:start v=1 -->\nold\n<!-- orchd:managed:end -->" + suffix)
        con = migrations.connect(self.home, self.env)
        with con:
            folders = migrations.targets(self.home, self.env, con)
            scope = migrations.json.dumps({r: str(f.absolute()) for r, f in folders.items()}, sort_keys=True)
            con.execute("INSERT INTO applied VALUES('orch-rules-v1', ?, 0)", (scope,))
        con.close()
        self.assertEqual(self.run_migrations()[0], 0)
        content = self.file.read_bytes()
        self.assertTrue(content.startswith(prefix) and content.endswith(suffix))
        self.assertIn("polling".encode(), content)
        self.assertIn(b"orchd:managed:start v=2", content)
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
        self.assertIn("+<!-- orchd:managed:start v=2 -->", "\n".join(lines))
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
        self.assertEqual(len(self.applied()), 4)  # 2 steps x 2 scopes; step completion is scoped to home paths

    def test_cli_dry_run_and_migrations_only_need_no_uv_install(self):
        env = {**{k: v for k, v in os.environ.items() if not k.startswith("ORCHD_")}, **self.env, "ORCHD_ROOT": str(self.home / "orch")}
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


class StartupMigrationTest(unittest.TestCase):
    """NatChung/orchd#58: installing a new version without `orchd upgrade` must not leave stale Orch rules."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.env = {"ORCHD_HOME": str(self.home / "state")}
        self.file = self.home / "orch/home/AGENTS.md"
        self.prefix, self.suffix = b"mine before\n", b"\nmine after"

    def stale(self):
        """Installed + init'd on the v1 rules; a newer program (v2 step) was installed without `orchd upgrade`."""
        self.file.parent.mkdir(parents=True)
        self.file.write_bytes(self.prefix + b"<!-- orchd:managed:start v=1 -->\nold\n<!-- orchd:managed:end -->" + self.suffix)
        migrations.record_init(self.home, self.env)
        con = migrations.connect(self.home, self.env)
        with con:
            folders = migrations.targets(self.home, self.env, con)
            scope = migrations.json.dumps({r: str(f.absolute()) for r, f in folders.items()}, sort_keys=True)
            con.execute("INSERT INTO applied VALUES('orch-rules-v1', ?, 0)", (scope,))
        con.close()

    def ensure(self, env=None):
        return migrations.ensure_current(home=self.home, env=env or self.env)

    def backups(self):
        return list((self.home / "state/backups/home").glob("AGENTS.md.*"))

    def test_stale_home_is_applied_on_startup_with_backup_and_outside_bytes_kept(self):
        self.stale()
        before = self.file.read_bytes()
        lines = self.ensure()
        content = self.file.read_bytes()
        self.assertTrue(content.startswith(self.prefix) and content.endswith(self.suffix))
        self.assertIn(b"orchd:managed:start v=2", content)
        self.assertEqual([b.read_bytes() for b in self.backups()], [before])
        self.assertIn("restart", "\n".join(lines).lower())

    def test_up_to_date_startup_writes_nothing_and_says_nothing(self):
        self.stale()
        self.ensure()
        snapshot = {p: p.stat().st_mtime_ns for p in self.home.rglob("*") if p.is_file()}
        self.assertEqual(self.ensure(), [])
        self.assertEqual({p: p.stat().st_mtime_ns for p in self.home.rglob("*") if p.is_file()}, snapshot)
        self.assertEqual(len(self.backups()), 1)

    def test_never_initialised_machine_is_left_alone(self):
        self.assertEqual(self.ensure(), [])
        self.assertEqual(list(self.home.rglob("*")), [])

    def test_opt_out_warns_without_writing(self):
        self.stale()
        before = self.file.read_bytes()
        lines = self.ensure({**self.env, "ORCHD_AUTO_MIGRATE": "0"})
        self.assertEqual(self.file.read_bytes(), before)
        self.assertEqual(self.backups(), [])
        self.assertIn("orchd upgrade --migrations-only", "\n".join(lines))

    def test_apply_failure_warns_and_never_raises(self):
        self.stale()
        self.file.write_bytes(b"<!-- orchd:managed:start v=1 -->unfinished")
        lines = self.ensure()
        self.assertEqual(self.file.read_bytes(), b"<!-- orchd:managed:start v=1 -->unfinished")
        self.assertIn("orchd upgrade --migrations-only", "\n".join(lines))

    def test_concurrent_startups_apply_once(self):
        import threading
        self.stale()
        threads = [threading.Thread(target=self.ensure) for _ in range(4)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(len(self.backups()), 1)
        self.assertEqual(self.file.read_bytes().count(b"orchd:managed:start"), 1)

    def isolated_env(self, **extra):
        """Child env that cannot reach outside the fixture: no inherited ORCHD_* override survives."""
        env = {k: v for k, v in os.environ.items() if not k.startswith("ORCHD_")}
        env.update(HOME=str(self.home), ORCHD_HOME=str(self.home / "state"), ORCHD_AUTO_MIGRATE="1", **extra)
        return env

    def tree(self):
        return {p: p.read_bytes() for p in self.home.rglob("*") if p.is_file() and ".db" not in p.name}

    def test_mcp_server_startup_applies_stale_rules(self):
        self.stale()
        out = subprocess.run([sys.executable, "bin/orchd", "mcp"], input="", env=self.isolated_env(),
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn(b"orchd:managed:start v=2", self.file.read_bytes())
        self.assertEqual(out.stdout, "")  # stdout is the MCP protocol channel
        self.assertIn("restart", out.stderr.lower())

    def test_inherited_home_overrides_never_write_outside_the_fixture(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        elsewhere = Path(outside.name)
        (elsewhere / "AGENTS.md").write_bytes(b"outside\n<!-- orchd:managed:start v=1 -->\nold\n<!-- orchd:managed:end -->")
        before = {p: p.read_bytes() for p in elsewhere.rglob("*") if p.is_file()}
        self.stale()
        with patch.dict(os.environ, {"ORCHD_ROOT": outside.name, "ORCHD_ORCH_HOME": outside.name,
                                     "ORCHD_INTERFACE_HOME": outside.name, "ORCHD_AUTO_MIGRATE": "1"}):
            env = self.isolated_env()
        self.assertFalse([k for k in env if k.startswith("ORCHD_") and k not in ("ORCHD_HOME", "ORCHD_AUTO_MIGRATE")])
        out = subprocess.run([sys.executable, "bin/orchd", "mcp"], input="", env=env, capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual({p: p.read_bytes() for p in elsewhere.rglob("*") if p.is_file()}, before)

    def test_read_only_commands_never_apply_home_migrations(self):
        """dry-run and read-only commands must leave files, backups and migration state untouched (#58)."""
        cases = [["orch", "restart", "--dry-run"], ["binding", "--status"], ["stats"],
                 ["intake", "backlog"], ["intake", "pending"], ["intake", "decisions"],
                 ["summary"], ["watch"]]  # summary/watch are read-only (orchd/watch.py)
        for argv in cases:
            with self.subTest(argv=argv):
                self.home_reset()
                self.stale()
                before = self.tree()
                state_before = migrations.pending(self.home, self.isolated_env())
                try:  # watch never exits on its own
                    subprocess.run([sys.executable, "bin/orchd", *argv], input="", env=self.isolated_env(),
                                   capture_output=True, text=True, timeout=8)
                except subprocess.TimeoutExpired:
                    pass
                self.assertEqual(self.tree(), before)
                self.assertEqual(self.backups(), [])
                self.assertEqual(migrations.pending(self.home, self.isolated_env()), state_before)

    def test_missing_state_db_is_silent_even_if_exists_is_mocked(self):
        """A fresh machine has no migration DB: no error, no files, even when Path.exists lies."""
        env, nowhere = self.isolated_env(), Path(self.home) / "nowhere"
        with patch("pathlib.Path.exists", return_value=True):
            self.assertEqual(migrations.pending(nowhere, env), [])
            self.assertEqual(migrations.ensure_current(home=nowhere, env=env), [])

    def test_mutating_command_still_applies_home_migrations(self):
        self.stale()
        subprocess.run([sys.executable, "bin/orchd", "ack", "nosuchtask"], input="", env=self.isolated_env(),
                       capture_output=True, text=True)
        self.assertIn(b"orchd:managed:start v=2", self.file.read_bytes())

    def test_early_return_mutating_entries_apply_home_migrations(self):
        """goal add/set and entry send/ask/tag return before any later hook; they must still migrate (#59)."""
        cases = [["goal", "add", "example"], ["goal", "set", "g1"],
                 ["entry", "send", "--text", "hi"], ["entry", "ask", "--text", "hi"], ["entry", "tag", "1", "--issue-ref", "x#1"]]
        for argv in cases:
            with self.subTest(argv=argv):
                self.home_reset()
                self.stale()
                subprocess.run([sys.executable, "bin/orchd", *argv], input="", env=self.isolated_env(),
                               capture_output=True, text=True)
                self.assertIn(b"orchd:managed:start v=2", self.file.read_bytes())
                self.assertEqual(len(self.backups()), 1)

    def test_early_return_read_only_entries_only_warn(self):
        cases = [["goal", "list"], ["goal", "show", "g1"], ["goal", "export", "r"], ["board", "/dev/null"],
                 ["snapshot"], ["list"], ["entry", "thread", "--issue-ref", "x#1"], ["entry-question", "show", "1"]]
        for argv in cases:
            with self.subTest(argv=argv):
                self.home_reset()
                self.stale()
                before, state = self.file.read_bytes(), migrations.pending(self.home, self.isolated_env())
                subprocess.run([sys.executable, "bin/orchd", *argv], input="", env=self.isolated_env(),
                               capture_output=True, text=True)
                self.assertEqual(self.file.read_bytes(), before)  # other files (e.g. a probed CLI's own dotfile) are not home rules
                self.assertEqual(migrations.pending(self.home, self.isolated_env()), state)
                self.assertEqual(self.backups(), [])

    def test_every_cli_subcommand_has_a_home_migration_policy(self):
        """New subcommands must be classified (write / warn / skip) or this fails."""
        import argparse
        from orchd import cli

        def leaves(parser, path=()):
            subs = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
            if not subs:
                yield path
                return
            for name, child in subs[0].choices.items():
                yield from leaves(child, path + (name,))
        found = set(leaves(cli.build_parser()))
        self.assertTrue(found)
        self.assertEqual(found - set(cli.HOME_POLICY), set(), "unclassified subcommands")
        self.assertEqual(set(cli.HOME_POLICY) - found, set(), "policy entries for removed subcommands")
        self.assertLessEqual(set(cli.HOME_POLICY.values()), {"write", "warn", "skip"})
        self.assertEqual(cli.HOME_POLICY[("init",)], "skip")
        self.assertEqual(cli.HOME_POLICY[("upgrade",)], "skip")

    def home_reset(self):
        import shutil
        for child in self.home.iterdir():
            shutil.rmtree(child) if child.is_dir() else child.unlink()


class IntakeRuleTemplateTest(unittest.TestCase):
    def test_home_template_states_entry_versus_intake_handling(self):
        text = (bootstrap.TEMPLATES / "home/AGENTS.md").read_text(encoding="utf-8")
        block = text[text.index("<!-- orchd:managed:start"):text.index("<!-- orchd:managed:end -->")]
        for needle in ("[orchd intake]", "polling", "指令", "不回覆", "不逐則問", "決策卡", "intake_pending"):
            self.assertIn(needle, block)
        self.assertIn("v=2", block.splitlines()[0])
