import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from orchd import doctor

HELP_FULL = "Usage: claude\n  --bg, --background\n  --messaging-socket-path <p>\n"
HELP_BG = "Usage: claude\n  --bg, --background\n"
SECRET = "sk-ant-SECRET-TOKEN-123"


def make_runner(overrides=None):
    """Fake CLIs keyed by the command tail; an Exception value is raised, a tuple is (rc, out)."""
    table = {
        ("git", "--version"): (0, "git version 2"),
        ("claude", "--version"): (0, "2.1.287 (Claude Code)"),
        ("claude", "--help"): (0, HELP_FULL),
        ("claude", "auth", "status"): (0, json.dumps({"loggedIn": True, "token": SECRET})),
        ("codex", "--version"): (0, "codex-cli 0.159"),
        ("codex", "login", "status"): (0, "Logged in using ChatGPT"),
        ("gh", "auth", "status"): (0, "  ✓ Logged in to github.com account NatChung (keyring)\n"),
    }
    table.update(overrides or {})

    def runner(cmd, timeout=15):
        key = (Path(cmd[0]).name, *cmd[1:])
        if key not in table:
            raise FileNotFoundError(cmd[0])
        value = table[key]
        if isinstance(value, Exception):
            raise value
        return value
    return runner


class DoctorCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.orch = self.home / "projects" / "orch"
        self.orch.mkdir(parents=True)
        (self.orch / "AGENTS.md").write_text("orch")
        (self.home / "projects" / "repoA" / ".git").mkdir(parents=True)
        (self.home / "projects" / "repoB" / ".git").mkdir(parents=True)
        self.write_claude_json({str(self.orch): True, str(self.home / "projects" / "repoA"): True})
        (self.home / ".codex").mkdir()
        (self.home / ".codex" / "config.toml").write_text(
            f'[projects."{self.orch}"]\ntrust_level = "trusted"\n')
        self.sock_root = Path(self.tmp.name) / "tmp"
        self.sock_root.mkdir()

    def write_claude_json(self, trust):
        (self.home / ".claude.json").write_text(json.dumps(
            {"projects": {p: {"hasTrustDialogAccepted": v} for p, v in trust.items()}}))

    def run_doctor(self, overrides=None, which=None, profile=None):
        d = doctor.Doctor(runner=make_runner(overrides), home=self.home, env={},
                          tmp_root=str(self.sock_root), data_dir=self.home / "data",
                          which=which or (lambda n: n), profile=profile)
        d.run_all()
        return d, {c.name: c for c in d.checks}


class RequiredChecks(DoctorCase):
    def test_healthy_machine_has_no_required_failure(self):
        d, by = self.run_doctor()
        bad = [c.name for c in d.checks if c.severity == doctor.REQUIRED and c.status != doctor.PASS]
        self.assertEqual(bad, [])
        self.assertEqual(doctor.exit_code(d.checks), 0)

    def test_missing_claude_cli_is_required_fail(self):
        d, by = self.run_doctor({("claude", "--version"): FileNotFoundError("claude")})
        self.assertEqual(by["claude CLI"].status, doctor.FAIL)
        self.assertEqual(doctor.exit_code(d.checks), 1)

    def test_nonzero_exit_is_fail_not_pass(self):
        d, by = self.run_doctor({("claude", "--version"): (1, "boom")})
        self.assertEqual(by["claude CLI"].status, doctor.FAIL)

    def test_runtime_error_and_timeout_are_unknown_never_pass(self):
        for exc in (subprocess.TimeoutExpired("claude", 15), RuntimeError("x")):
            d, by = self.run_doctor({("claude", "--version"): exc})
            self.assertEqual(by["claude CLI"].status, doctor.UNKNOWN)

    def test_unknown_required_alone_exits_2_and_is_listed(self):
        d, by = self.run_doctor({("claude", "--help"): (0, HELP_BG)})
        self.assertEqual(by["claude --messaging-socket-path"].status, doctor.UNKNOWN)
        self.assertEqual(doctor.exit_code(d.checks), 2)
        self.assertIn("claude --messaging-socket-path", doctor.render(d.checks).split("Unknown")[-1])

    def test_fail_beats_unknown_in_exit_code(self):
        d, _ = self.run_doctor({("claude", "--help"): (0, "Usage: claude\n")})
        self.assertEqual(doctor.exit_code(d.checks), 1)

    def test_claude_without_bg_fails(self):
        _, by = self.run_doctor({("claude", "--help"): (0, "Usage: claude\n")})
        self.assertEqual(by["claude --bg"].status, doctor.FAIL)

    def test_permission_denied_cli_is_fail_without_bypass(self):
        _, by = self.run_doctor({("claude", "--version"): PermissionError("x")})
        self.assertEqual(by["claude CLI"].status, doctor.FAIL)

    def test_not_logged_in_fails_and_unparseable_is_unknown(self):
        _, by = self.run_doctor({("claude", "auth", "status"): (1, json.dumps({"loggedIn": False}))})
        self.assertEqual(by["claude login"].status, doctor.FAIL)
        _, by = self.run_doctor({("claude", "auth", "status"): (0, "weird text")})
        self.assertEqual(by["claude login"].status, doctor.UNKNOWN)

    def test_orch_home_missing_and_untrusted(self):
        (self.orch / "AGENTS.md").unlink()
        _, by = self.run_doctor()
        self.assertEqual(by["orch home"].status, doctor.FAIL)
        self.write_claude_json({})
        _, by = self.run_doctor()
        self.assertEqual(by["orch home trusted in Claude"].status, doctor.FAIL)
        self.assertIn("trust prompt", by["orch home trusted in Claude"].fix)

    def test_unreadable_claude_json_is_unknown_not_bypassed(self):
        cj = self.home / ".claude.json"
        cj.chmod(0)
        self.addCleanup(cj.chmod, 0o600)
        if os.access(cj, os.R_OK):
            self.skipTest("running as a user that ignores file modes")
        _, by = self.run_doctor()
        self.assertEqual(by["orch home trusted in Claude"].status, doctor.UNKNOWN)

    def test_socket_probe_cleans_up_its_own_tmp_and_nothing_else(self):
        other = self.sock_root / "someone-elses"
        other.mkdir()
        self.run_doctor()
        self.assertEqual([p.name for p in self.sock_root.iterdir()], ["someone-elses"])

    def test_socket_probe_cleans_up_when_bind_fails(self):
        d = doctor.Doctor(runner=make_runner(), home=self.home, env={}, tmp_root=str(self.sock_root))
        long_root = self.sock_root / ("x" * 120)  # sun_path limit makes bind() raise
        long_root.mkdir()
        d.tmp_root = str(long_root)
        d.check_socket_dir()
        self.assertEqual(d.checks[0].status, doctor.FAIL)
        self.assertEqual(list(long_root.iterdir()), [])

    def test_socket_probe_unwritable_root_fails(self):
        d = doctor.Doctor(runner=make_runner(), home=self.home, env={}, tmp_root=str(self.sock_root / "nope"))
        d.check_socket_dir()
        self.assertEqual(d.checks[0].status, doctor.FAIL)

    def test_missing_projects_dir_fails(self):
        d = doctor.Doctor(runner=make_runner(), home=self.home, env={}, projects=self.home / "nope")
        d.check_repos_trust()
        self.assertEqual(d.checks[0].status, doctor.FAIL)


class OptionalChecks(DoctorCase):
    def test_missing_integrations_only_warn(self):
        d, by = self.run_doctor({("codex", "--version"): FileNotFoundError("codex"),
                                 ("gh", "auth", "status"): FileNotFoundError("gh")},
                                which=lambda n: None)
        for name in ("codex CLI", "gh accounts", "codegraph", "rtk", "gcloud", "fastlane"):
            self.assertEqual(by[name].status, doctor.WARN, name)
        self.assertEqual(doctor.exit_code(d.checks), 0)
        self.assertTrue(all(c.status != doctor.FAIL for c in d.checks if c.severity != doctor.REQUIRED))

    def test_codex_not_logged_in_warns(self):
        _, by = self.run_doctor({("codex", "login", "status"): (1, "Not logged in")})
        self.assertEqual(by["codex login"].status, doctor.WARN)

    def test_optional_probe_that_cannot_run_is_unknown_and_tool_still_runs(self):
        d, by = self.run_doctor({("gh", "auth", "status"): subprocess.TimeoutExpired("gh", 15),
                                 ("codex", "login", "status"): RuntimeError("x")})
        self.assertEqual(by["gh accounts"].status, doctor.UNKNOWN)
        self.assertEqual(by["codex login"].status, doctor.UNKNOWN)
        self.assertEqual(doctor.exit_code(d.checks), 0)

    def test_untrusted_repos_listed(self):
        _, by = self.run_doctor()
        self.assertEqual(by["repos trusted in Claude"].status, doctor.WARN)
        self.assertIn("repoB", by["repos trusted in Claude"].detail)
        self.assertNotIn("repoA", by["repos trusted in Claude"].detail)

    def test_codex_config_with_other_machines_home_warns(self):
        (self.home / ".codex" / "config.toml").write_text(
            f'[projects."{self.orch}"]\ntrust_level = "trusted"\n\n'
            '[projects."/Users/someoneelse/projects/x"]\ntrust_level = "trusted"\n')
        _, by = self.run_doctor()
        self.assertEqual(by["codex config paths match this home"].status, doctor.WARN)
        self.assertEqual(by["orch home trusted in Codex"].status, doctor.PASS)

    def test_gh_partial_accounts_pass_with_broken_listed(self):
        out = ("  ✓ Logged in to github.com account NatChung (keyring)\n"
               "  X Failed to log in to github.com account nat862 (default)\n")
        _, by = self.run_doctor({("gh", "auth", "status"): (1, out)})
        self.assertEqual(by["gh accounts"].status, doctor.WARN)
        self.assertIn("usable: NatChung", by["gh accounts"].detail)
        self.assertIn("broken: nat862", by["gh accounts"].detail)


class ProfileAndSecrets(DoctorCase):
    def test_nat_profile_is_off_by_default_and_never_required(self):
        _, by = self.run_doctor()
        self.assertFalse([n for n in by if n.startswith("nat:")])
        d, by = self.run_doctor(profile="nat")
        nat = [c for c in d.checks if c.name.startswith("nat:")]
        self.assertTrue(nat)
        self.assertTrue(all(c.severity == doctor.PROFILE and c.status != doctor.FAIL for c in nat))
        self.assertEqual(doctor.exit_code(d.checks), 0)

    def test_nat_profile_checks_existence_only(self):
        ssh = self.home / ".ssh"
        ssh.mkdir()
        (ssh / "config").write_text("Host github-nat862\nHost github-NatChung\nHost github-natchung-kc\n")
        cfg = self.home / ".config" / "ariontechs-ops"
        cfg.mkdir(parents=True)
        (cfg / "token-a.json").write_text(SECRET)
        d, by = self.run_doctor(profile="nat")
        self.assertEqual(by["nat: ssh host aliases"].status, doctor.PASS)
        self.assertEqual(by["nat: email tokens"].status, doctor.PASS)
        self.assertNotIn(SECRET, json.dumps([c.as_dict() for c in d.checks]))

    def test_output_never_contains_secrets(self):
        d, _ = self.run_doctor({("claude", "auth", "status"): (0, json.dumps(
            {"loggedIn": True, "email": "me@example.com", "token": SECRET}))})
        text = doctor.render(d.checks) + json.dumps([c.as_dict() for c in d.checks])
        self.assertNotIn(SECRET, text)
        self.assertNotIn("me@example.com", text)

    def test_doctor_does_not_write_outside_its_probe(self):
        before = sorted(p.relative_to(self.home) for p in self.home.rglob("*"))
        self.run_doctor(profile="nat")
        self.assertEqual(sorted(p.relative_to(self.home) for p in self.home.rglob("*")), before)


if __name__ == "__main__":
    unittest.main()
