import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchd import doctor

HELP_FULL = "Usage: claude\n  --bg, --background\n  --messaging-socket-path <p>\n"
HELP_BG = "Usage: claude\n  --bg, --background\n"
SECRET = "sk-ant-SECRET-TOKEN-123"
needs_toml = unittest.skipIf(doctor.tomllib is None, "no tomllib on this Python; doctor reports unknown")


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
        self.orch = self.home / "orch" / "home"
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


class OrchdCommandCheck(DoctorCase):
    def test_reports_install_checkout_or_missing(self):
        _, by = self.run_doctor(which=lambda n: None if n == "orchd" else n)
        self.assertEqual(by["orchd on PATH"].status, doctor.WARN)
        self.assertIn("uv tool install", by["orchd on PATH"].fix)
        checkout = Path(self.tmp.name) / "co"
        (checkout / "bin").mkdir(parents=True)
        (checkout / "orchd").mkdir()
        (checkout / "orchd" / "cli.py").write_text("")
        (checkout / "bin" / "orchd").write_text("")
        _, by = self.run_doctor(which=lambda n: str(checkout / "bin" / "orchd") if n == "orchd" else n)
        self.assertEqual(by["orchd on PATH"].status, doctor.PASS)
        self.assertIn("checkout", by["orchd on PATH"].detail)
        tool = Path(self.tmp.name) / "tools" / "bin" / "orchd"
        tool.parent.mkdir(parents=True)
        tool.write_text("")
        _, by = self.run_doctor(which=lambda n: str(tool) if n == "orchd" else n)
        self.assertIn("installed", by["orchd on PATH"].detail)


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
        d, by = self.run_doctor({("claude", "--help"): RuntimeError("help crashed")})
        self.assertEqual(by["claude --messaging-socket-path"].status, doctor.UNKNOWN)
        self.assertEqual(by["claude --messaging-socket-path"].severity, doctor.REQUIRED)
        self.assertEqual(doctor.exit_code(d.checks), 2)
        self.assertIn("claude --messaging-socket-path", doctor.render(d.checks).split("Unknown")[-1])

    def test_hidden_socket_flag_is_unknown_but_does_not_block(self):
        d, by = self.run_doctor({("claude", "--help"): (0, HELP_BG)})
        self.assertEqual((by["claude --messaging-socket-path"].status, by["claude --messaging-socket-path"].severity),
                         (doctor.UNKNOWN, doctor.OPTIONAL))
        self.assertEqual(doctor.exit_code(d.checks), 0)

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

    def test_fresh_install_before_init_is_not_a_failure(self):
        import shutil
        shutil.rmtree(self.orch)
        d, by = self.run_doctor()
        self.assertEqual(by["orch home"].status, doctor.WARN)
        self.assertIn("orchd init", by["orch home"].fix)
        self.assertEqual(doctor.exit_code(d.checks), 0)
        self.assertIn("required checks: all pass", doctor.render(d.checks))
        self.orch.write_text("not a folder")
        _, by = self.run_doctor()
        self.assertEqual(by["orch home"].status, doctor.FAIL)

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

    def test_custom_projects_config_avoids_required_failure(self):
        custom = self.home / "GitProjects"
        (self.home / "projects").rename(custom)
        config = self.home / ".config" / "orchd"
        config.mkdir(parents=True)
        (config / "config.toml").write_text('projects_dir = "~/GitProjects"\n')
        d, by = self.run_doctor()
        self.assertEqual(by["projects dir"].status, doctor.PASS)
        self.assertEqual(d.projects, custom)
        self.assertEqual(doctor.exit_code(d.checks), 0)

    def test_checkout_parent_is_detected_without_default_projects(self):
        custom = self.home / "GitProjects"
        (self.home / "projects").rename(custom)
        checkout = custom / "orchd"
        (checkout / ".git").mkdir(parents=True)
        with mock.patch("orchd.paths.__file__", str(checkout / "orchd" / "paths.py")):
            d, by = self.run_doctor()
        self.assertEqual(by["projects dir"].status, doctor.PASS)
        self.assertEqual(d.projects, custom)
        self.assertEqual(doctor.exit_code(d.checks), 0)

    def test_missing_projects_dir_fails(self):
        d = doctor.Doctor(runner=make_runner(), home=self.home, env={}, projects=self.home / "nope")
        d.check_repos_trust()
        self.assertEqual(d.checks[0].status, doctor.FAIL)


class Boundaries(DoctorCase):
    """Review f291526e: source missing/malformed/wrong-shaped is unknown; a wrong file type is a known fail;
    one broken source never stops the other checks and never turns into a pass."""

    def trust(self, by):
        return by["orch home trusted in Claude"]

    def test_missing_claude_json_is_unknown_not_fail(self):
        (self.home / ".claude.json").unlink()
        d, by = self.run_doctor()
        self.assertEqual(self.trust(by).status, doctor.UNKNOWN)
        self.assertIn("missing", self.trust(by).detail)
        self.assertEqual(doctor.exit_code(d.checks), 2)

    def test_malformed_claude_json_is_unknown(self):
        (self.home / ".claude.json").write_text("{not json")
        _, by = self.run_doctor()
        self.assertEqual(self.trust(by).status, doctor.UNKNOWN)
        self.assertEqual(by["repos trusted in Claude"].status, doctor.UNKNOWN)

    def test_claude_json_wrong_shapes_do_not_crash(self):
        for body in ('[]', '"x"', '{"projects": []}', '{"projects": "x"}',
                     json.dumps({"projects": {str(self.orch): "x"}})):
            (self.home / ".claude.json").write_text(body)
            _, by = self.run_doctor()
            self.assertEqual(self.trust(by).status, doctor.UNKNOWN, body)

    def test_trust_flag_must_be_a_real_boolean(self):
        for value, status in ((True, doctor.PASS), (False, doctor.FAIL), ("false", doctor.UNKNOWN),
                              ("true", doctor.UNKNOWN), (1, doctor.UNKNOWN), (None, doctor.UNKNOWN)):
            (self.home / ".claude.json").write_text(json.dumps(
                {"projects": {str(self.orch): {"hasTrustDialogAccepted": value}}}))
            _, by = self.run_doctor()
            self.assertEqual(self.trust(by).status, status, repr(value))

    def test_absent_project_entry_is_known_untrusted(self):
        (self.home / ".claude.json").write_text(json.dumps({"projects": {}}))
        _, by = self.run_doctor()
        self.assertEqual(self.trust(by).status, doctor.FAIL)

    @needs_toml
    def test_codex_comment_does_not_count_as_trust(self):
        (self.home / ".codex" / "config.toml").write_text(
            f'[projects."{self.orch}"]\n# trust_level = "trusted"\ntrust_level = "untrusted"\n')
        _, by = self.run_doctor()
        self.assertEqual(by["orch home trusted in Codex"].status, doctor.WARN)

    def test_invalid_toml_is_unknown_not_pass(self):
        (self.home / ".codex" / "config.toml").write_text(
            f'[projects."{self.orch}"\ntrust_level = "trusted"\n')
        _, by = self.run_doctor()
        self.assertEqual(by["orch home trusted in Codex"].status, doctor.UNKNOWN)
        self.assertEqual(by["config paths match this home"].status, doctor.UNKNOWN)

    def test_no_tomllib_is_unknown_never_regex_pass(self):
        real = doctor.tomllib
        doctor.tomllib = None
        self.addCleanup(setattr, doctor, "tomllib", real)
        _, by = self.run_doctor()
        self.assertEqual(by["orch home trusted in Codex"].status, doctor.UNKNOWN)
        self.assertIn("tomllib", by["orch home trusted in Codex"].detail)
        self.assertEqual(by["config paths match this home"].status, doctor.UNKNOWN)

    @needs_toml
    def test_codex_mcp_command_with_old_home_is_flagged_without_secrets(self):
        (self.home / ".codex" / "config.toml").write_text(
            f'[projects."{self.orch}"]\ntrust_level = "trusted"\n\n'
            '[mcp_servers.cg]\ncommand = "/Users/oldmac/.local/bin/codegraph"\n'
            f'args = ["--token", "{SECRET}"]\nenv = {{ KEY = "{SECRET}" }}\n')
        d, by = self.run_doctor()
        c = by["config paths match this home"]
        self.assertEqual(c.status, doctor.WARN)
        self.assertIn("/Users/oldmac", c.detail)
        self.assertIn("codex MCP cg", c.detail)
        self.assertNotIn(SECRET, doctor.render(d.checks) + json.dumps([x.as_dict() for x in d.checks]))

    def test_claude_user_and_project_mcp_paths_are_checked(self):
        (self.home / ".claude.json").write_text(json.dumps({
            "projects": {str(self.orch): {"hasTrustDialogAccepted": True,
                                          "mcpServers": {"p": {"command": "node", "args": ["/home/olduser/s.js"]}}}}}))
        _, by = self.run_doctor()
        self.assertEqual(by["config paths match this home"].status, doctor.WARN)
        self.assertIn("/home/olduser", by["config paths match this home"].detail)
        (self.home / ".claude.json").write_text(json.dumps({
            "projects": {}, "mcpServers": {"u": {"command": "/Users/oldmac/bin/x"}}}))
        _, by = self.run_doctor()
        self.assertIn("claude user MCP u", by["config paths match this home"].detail)

    @needs_toml
    def test_mcp_under_this_home_passes_and_names_what_was_checked(self):
        (self.home / ".codex" / "config.toml").write_text(
            f'[projects."{self.orch}"]\ntrust_level = "trusted"\n\n'
            f'[mcp_servers.cg]\ncommand = "{self.home}/.local/bin/codegraph"\n')
        _, by = self.run_doctor()
        c = by["config paths match this home"]
        self.assertEqual(c.status, doctor.PASS)
        self.assertIn("MCP", c.detail)

    def test_projects_path_that_is_a_regular_file_fails_and_others_continue(self):
        f = self.home / "projfile"
        f.write_text("x")
        d = doctor.Doctor(runner=make_runner(), home=self.home, env={}, projects=f, tmp_root=str(self.sock_root),
                          data_dir=self.home / "data", which=lambda n: n)
        d.run_all()
        by = {c.name: c for c in d.checks}
        self.assertEqual(by["projects dir"].status, doctor.FAIL)
        self.assertIn("socket dir (0700)", by)
        self.assertIn("orchd data dir", by)
        self.assertEqual(doctor.exit_code(d.checks), 1)

    def test_data_dir_that_is_a_regular_file_fails(self):
        f = self.home / "datafile"
        f.write_text("user data")
        d = doctor.Doctor(runner=make_runner(), home=self.home, env={}, data_dir=f)
        d.check_data_dir()
        self.assertEqual(d.checks[0].status, doctor.FAIL)
        self.assertEqual(f.read_text(), "user data")  # user data untouched
        g = self.home / "under-file" / "data"
        (self.home / "under-file").write_text("x")
        d = doctor.Doctor(runner=make_runner(), home=self.home, env={}, data_dir=g)
        d.check_data_dir()
        self.assertEqual(d.checks[0].status, doctor.FAIL)

    def test_orch_home_that_is_a_file_fails(self):
        f = self.home / "orchfile"
        f.write_text("x")
        d = doctor.Doctor(runner=make_runner(), home=self.home, env={}, orch_home=f)
        d.check_orch_home()
        self.assertEqual(d.checks[0].status, doctor.FAIL)

    def test_empty_connector_dirs_and_token_dirs_are_not_credentials(self):
        conn = self.home / "projects" / "nat-assistant" / "connectors"
        (conn / "slack-tools").mkdir(parents=True)
        (conn / "line-tools").mkdir(parents=True)
        d, by = self.run_doctor(profile="nat")
        for name in ("nat: slack tokens", "nat: line token", "nat: email tokens"):
            self.assertEqual(by[name].status, doctor.WARN, name)
        cfg = self.home / ".config" / "ariontechs-ops"
        (cfg / "slack-token-x.json").mkdir(parents=True)  # a directory is not a token
        (cfg / "line-token.json").mkdir()
        _, by = self.run_doctor(profile="nat")
        self.assertEqual(by["nat: slack tokens"].status, doctor.WARN)
        self.assertEqual(by["nat: line token"].status, doctor.WARN)


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

    @needs_toml
    def test_codex_config_with_other_machines_home_warns(self):
        (self.home / ".codex" / "config.toml").write_text(
            f'[projects."{self.orch}"]\ntrust_level = "trusted"\n\n'
            '[projects."/Users/someoneelse/projects/x"]\ntrust_level = "trusted"\n')
        _, by = self.run_doctor()
        self.assertEqual(by["config paths match this home"].status, doctor.WARN)
        self.assertEqual(by["orch home trusted in Codex"].status, doctor.PASS)

    def test_gh_partial_accounts_pass_with_broken_listed(self):
        out = ("  ✓ Logged in to github.com account NatChung (keyring)\n"
               "  X Failed to log in to github.com account nat862 (default)\n")
        _, by = self.run_doctor({("gh", "auth", "status"): (1, out)})
        self.assertEqual(by["gh accounts"].status, doctor.WARN)
        self.assertIn("usable: NatChung", by["gh accounts"].detail)
        self.assertIn("broken: nat862", by["gh accounts"].detail)


class EncodingAndSchema(DoctorCase):
    """Review of 2b1cdb6: non-UTF-8 bytes in any source must be unknown for that source and never stop later
    checks; wrong-typed MCP containers/servers/fields must be unknown, not skipped into a pass. Valid shapes
    (URL servers, absent optional fields) stay pass."""

    BAD = b"\xff\xfe[\x00p\x00]\x00"

    def healthy_names(self, profile=None):
        d, _ = self.run_doctor(profile=profile)
        return [c.name for c in d.checks]

    def assert_completes(self, d, profile=None):
        self.assertEqual([c.name for c in d.checks], self.healthy_names(profile))

    def paths(self, by):
        return by["config paths match this home"]

    def leaked(self, d):
        return SECRET in doctor.render(d.checks) + json.dumps([c.as_dict() for c in d.checks])

    def write_mcp(self, servers, where="user"):
        """Claude-only source: the Codex config is removed so a Python without tomllib cannot mask the result."""
        (self.home / ".codex" / "config.toml").unlink(missing_ok=True)
        body = {"projects": {str(self.orch): {"hasTrustDialogAccepted": True}}}
        if where == "user":
            body["mcpServers"] = servers
        else:
            body["projects"][str(self.orch)]["mcpServers"] = servers
        (self.home / ".claude.json").write_text(json.dumps(body))

    def test_non_utf8_orch_agents_is_unknown_and_later_checks_run(self):
        (self.orch / "AGENTS.md").write_bytes(self.BAD)
        d, by = self.run_doctor()
        self.assertEqual(by["orch home"].status, doctor.UNKNOWN)
        self.assertIn("UTF-8", by["orch home"].detail)
        self.assertEqual(by["orch home trusted in Claude"].status, doctor.PASS)
        self.assert_completes(d)
        self.assertEqual(doctor.exit_code(d.checks), 2)
        self.assertEqual((self.orch / "AGENTS.md").read_bytes(), self.BAD)  # user data untouched

    def test_non_utf8_codex_config_is_unknown_for_trust_and_paths(self):
        cfg = self.home / ".codex" / "config.toml"
        cfg.write_bytes(self.BAD)
        d, by = self.run_doctor()
        self.assertEqual(by["orch home trusted in Codex"].status, doctor.UNKNOWN)
        self.assertIn("UTF-8", by["orch home trusted in Codex"].detail)
        self.assertEqual(self.paths(by).status, doctor.UNKNOWN)  # claude alone must not make it a pass
        self.assert_completes(d)
        self.assertEqual(cfg.read_bytes(), self.BAD)

    def test_unreadable_codex_config_is_unknown_for_paths_too(self):
        real = Path.read_text

        def deny(path, *a, **kw):
            if path.name == "config.toml":
                raise PermissionError(13, "denied")
            return real(path, *a, **kw)
        with mock.patch.object(Path, "read_text", deny):
            d, by = self.run_doctor()
        self.assertEqual(by["orch home trusted in Codex"].status, doctor.UNKNOWN)
        self.assertEqual(self.paths(by).status, doctor.UNKNOWN)
        self.assert_completes(d)

    def test_non_utf8_ssh_config_is_unknown_and_profile_continues(self):
        (self.home / ".ssh").mkdir()
        (self.home / ".ssh" / "config").write_bytes(self.BAD)
        d, by = self.run_doctor(profile="nat")
        self.assertEqual(by["nat: ssh host aliases"].status, doctor.UNKNOWN)
        self.assert_completes(d, profile="nat")
        self.assertEqual(doctor.exit_code(d.checks), 0)  # profile never decides the exit code

    def test_non_utf8_claude_json_is_unknown_and_labelled_as_encoding(self):
        (self.home / ".claude.json").write_bytes(self.BAD)
        d, by = self.run_doctor()
        self.assertEqual(by["orch home trusted in Claude"].status, doctor.UNKNOWN)
        self.assertIn("UTF-8", by["orch home trusted in Claude"].detail)
        self.assertEqual(self.paths(by).status, doctor.UNKNOWN)
        self.assert_completes(d)

    def test_wrong_typed_claude_mcp_is_unknown_without_leaking_values(self):
        cases = {
            "container list": ([], "user"),
            "container str": (SECRET, "user"),
            "server str": ({"s": SECRET}, "user"),
            "server list": ({"s": [SECRET]}, "project"),
            "args str": ({"s": {"command": "node", "args": f"--token {SECRET}"}}, "user"),
            "args item int": ({"s": {"command": "node", "args": [1]}}, "project"),
            "args item dict": ({"s": {"command": "node", "args": [{"k": SECRET}]}}, "user"),
            "command int": ({"s": {"command": 5}}, "user"),
            "command list": ({"s": {"command": [SECRET]}}, "project"),
            "cwd dict": ({"s": {"command": "node", "cwd": {"p": SECRET}}}, "user"),
        }
        for label, (servers, where) in cases.items():
            self.write_mcp(servers, where)
            d, by = self.run_doctor()
            self.assertEqual(self.paths(by).status, doctor.UNKNOWN, label)
            self.assertFalse(self.leaked(d), label)
            self.assert_completes(d)

    def test_wrong_typed_claude_project_entry_is_unknown_for_paths(self):
        (self.home / ".codex" / "config.toml").unlink()
        (self.home / ".claude.json").write_text(json.dumps(
            {"projects": {str(self.orch): {"hasTrustDialogAccepted": True}, "/x": SECRET}}))
        d, by = self.run_doctor()
        self.assertEqual(self.paths(by).status, doctor.UNKNOWN)
        self.assertFalse(self.leaked(d))

    def test_valid_claude_mcp_shapes_stay_pass(self):
        servers = {
            "url": {"type": "http", "url": "https://example.com/mcp", "headers": {"Authorization": SECRET}},
            "sse": {"type": "sse", "url": "https://example.com/sse"},
            "bare": {"command": "npx"},
            "full": {"command": f"{self.home}/.local/bin/x", "args": ["-y", f"{self.home}/s.js"],
                     "cwd": str(self.home), "env": {"TOKEN": SECRET}},
            "empty args": {"command": "node", "args": []},
        }
        for where in ("user", "project"):
            self.write_mcp(servers, where)
            d, by = self.run_doctor()
            self.assertEqual(self.paths(by).status, doctor.PASS, where)
            self.assertFalse(self.leaked(d))
        self.write_mcp({})
        _, by = self.run_doctor()
        self.assertEqual(self.paths(by).status, doctor.PASS)

    def test_stale_path_still_warns_next_to_a_wrong_typed_sibling(self):
        self.write_mcp({"old": {"command": "/Users/oldmac/bin/x"}, "bad": {"command": 5}})
        _, by = self.run_doctor()
        self.assertEqual(self.paths(by).status, doctor.WARN)
        self.assertIn("/Users/oldmac", self.paths(by).detail)

    @needs_toml
    def test_wrong_typed_codex_mcp_is_unknown(self):
        trust = f'[projects."{self.orch}"]\ntrust_level = "trusted"\n\n'
        cases = {
            "container str": f'mcp_servers = "{SECRET}"\n' + trust,
            "container list": 'mcp_servers = [1]\n' + trust,
            "server str": trust + f'[mcp_servers]\ns = "{SECRET}"\n',
            "args str": trust + f'[mcp_servers.s]\ncommand = "node"\nargs = "--token {SECRET}"\n',
            "args item int": trust + '[mcp_servers.s]\ncommand = "node"\nargs = [1]\n',
            "command int": trust + '[mcp_servers.s]\ncommand = 5\n',
            "cwd array": trust + f'[mcp_servers.s]\ncommand = "node"\ncwd = ["{SECRET}"]\n',
            "projects str": f'projects = "{SECRET}"\n',
        }
        for label, text in cases.items():
            (self.home / ".codex" / "config.toml").write_text(text)
            d, by = self.run_doctor()
            self.assertEqual(self.paths(by).status, doctor.UNKNOWN, label)
            self.assertFalse(self.leaked(d), label)
            self.assert_completes(d)

    @needs_toml
    def test_valid_codex_mcp_shapes_stay_pass(self):
        (self.home / ".codex" / "config.toml").write_text(
            f'[projects."{self.orch}"]\ntrust_level = "trusted"\n\n'
            '[mcp_servers.url]\nurl = "https://example.com/mcp"\n'
            f'bearer_token_env_var = "TOKEN"\n\n'
            '[mcp_servers.bare]\ncommand = "npx"\n\n'
            f'[mcp_servers.full]\ncommand = "{self.home}/x"\nargs = ["-y", "{self.home}/s.js"]\n'
            f'cwd = "{self.home}"\nenv = {{ TOKEN = "{SECRET}" }}\n')
        d, by = self.run_doctor()
        self.assertEqual(by["orch home trusted in Codex"].status, doctor.PASS)
        self.assertEqual(self.paths(by).status, doctor.PASS)
        self.assertFalse(self.leaked(d))


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
        (cfg / "slack-token-a.json").write_text(SECRET)
        (cfg / "line-token.json").write_text(SECRET)
        d, by = self.run_doctor(profile="nat")
        self.assertEqual(by["nat: ssh host aliases"].status, doctor.PASS)
        for name in ("nat: email tokens", "nat: slack tokens", "nat: line token"):
            self.assertEqual(by[name].status, doctor.PASS, name)
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
