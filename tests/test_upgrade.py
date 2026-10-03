"""Issue #62: `orchd upgrade` reinstalls from uv's recorded source at the newest commit."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from orchd import upgrade

OLD, NEW = "a" * 40, "b" * 40


class UpgradeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.prefix = Path(self.tmp.name) / "tools" / "orchd"
        self.receipt('{ name = "orchd", git = "ssh://git@github-NatChung/NatChung/orchd" }')
        self.commit(OLD)
        self.calls = []
        self.head = NEW
        self.install_rc = 0

    def receipt(self, requirement):
        self.prefix.mkdir(parents=True, exist_ok=True)
        (self.prefix / "uv-receipt.toml").write_text(f"[tool]\nrequirements = [{requirement}]\n")

    def commit(self, sha):
        info = self.prefix / "lib" / "python3.13" / "site-packages" / "orchd-0.1.0.dist-info"
        info.mkdir(parents=True, exist_ok=True)
        (info / "direct_url.json").write_text(json.dumps(
            {"url": "ssh://git@github-NatChung/NatChung/orchd", "vcs_info": {"vcs": "git", "commit_id": sha}}))

    def run_cmd(self, cmd):
        self.calls.append(cmd)
        if cmd[:2] == ["git", "ls-remote"]:
            return subprocess.CompletedProcess(cmd, 0, f"{self.head}\tHEAD\n", "")
        if self.install_rc == 0:
            self.commit(self.head)
        return subprocess.CompletedProcess(cmd, self.install_rc, "", "uv: Repository not found")

    def upgrade(self, which=lambda name: "/opt/uv"):
        return upgrade.upgrade(self.prefix, run=self.run_cmd, which=which, checkout=False)

    def test_upgrades_from_the_recorded_source_and_reports_both_commits(self):
        code, lines = self.upgrade()
        self.assertEqual(code, 0)
        self.assertEqual(self.calls[-1], ["/opt/uv", "tool", "install", "--force", "--refresh",
                                          "git+ssh://git@github-NatChung/NatChung/orchd"])
        self.assertIn("orchd aaaaaaa -> bbbbbbb", lines)
        self.assertIn("orchd interface --new", lines[-1])

    def test_already_newest_does_not_reinstall(self):
        self.head = OLD
        code, lines = self.upgrade()
        self.assertEqual(code, 0)
        self.assertEqual([c[:2] for c in self.calls], [["git", "ls-remote"]])
        self.assertIn("already the newest", lines[0])

    def test_branch_pin_is_dropped_for_the_default_branch(self):
        self.receipt('{ name = "orchd", git = "ssh://git@github-NatChung/NatChung/orchd", rev = "doctor-before-init" }')
        code, lines = self.upgrade()
        self.assertEqual(code, 0)
        self.assertIn("rev=doctor-before-init", lines[0])
        self.assertEqual(self.calls[-1][-1], "git+ssh://git@github-NatChung/NatChung/orchd")

    def test_pin_in_the_url_is_dropped_too(self):
        self.receipt('{ name = "orchd", git = "ssh://git@github-NatChung/NatChung/orchd?rev=orchd-upgrade-62" }')
        code, lines = self.upgrade()
        self.assertEqual(code, 0)
        self.assertIn("rev=orchd-upgrade-62", lines[0])
        self.assertEqual(self.calls[-1][-1], "git+ssh://git@github-NatChung/NatChung/orchd")

    def test_failure_keeps_the_install_and_shows_uv_error(self):
        self.install_rc = 2
        code, lines = self.upgrade()
        self.assertEqual(code, 2)
        self.assertIn("Repository not found", lines[-1])
        self.assertEqual(upgrade.installed_commit(self.prefix), OLD)

    def test_without_uv_or_receipt_prints_what_to_run(self):
        code, lines = self.upgrade(which=lambda name: None)
        self.assertEqual(code, 1)
        self.assertIn("uv tool install --force --refresh git+ssh://", lines[-1])
        (self.prefix / "uv-receipt.toml").unlink()
        code, lines = self.upgrade()
        self.assertEqual(code, 1)
        self.assertIn("not installed with `uv tool install`", lines[0])

    def test_checkout_points_at_git_pull(self):
        bin_orchd = Path(self.tmp.name) / "co" / "bin" / "orchd"
        bin_orchd.parent.mkdir(parents=True)
        bin_orchd.write_text("")
        code, lines = upgrade.upgrade(self.prefix, run=self.run_cmd, which=lambda n: "/opt/uv", checkout=bin_orchd)
        self.assertEqual((code, self.calls), (1, []))
        self.assertIn("git pull", lines[0])


if __name__ == "__main__":
    unittest.main()
