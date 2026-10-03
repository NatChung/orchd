"""setup-wizard: plan content, zero mutation, HOME portability, no token leaks. Everything runs on a scratch HOME
with a fake runner; nothing real is installed, logged in or trusted."""
import hashlib
import importlib.util
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("setup_wizard", Path(__file__).resolve().parents[1] / "scripts" / "setup-wizard.py")
sw = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sw)

needs_toml = unittest.skipIf(sw.tomllib is None, "no tomllib on this Python; wizard reports TOML checks unknown")

SECRET = "gho_SECRETTOKEN1234567890"


def snapshot(root):
    out = {}
    for p in sorted(Path(root).rglob("*")):
        out[str(p)] = hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "dir"
    return out


class Fake:
    """Runner + which for a machine that has the listed tools. Mimics real CLI output shapes."""

    def __init__(self, tools=(), gh_accounts=(), claude_ok=True, codex_ok=True, gh_failed=(), pub_ok=True):
        self.tools, self.gh_accounts, self.claude_ok, self.codex_ok = set(tools), gh_accounts, claude_ok, codex_ok
        self.gh_failed, self.pub_ok = gh_failed, pub_ok
        self.calls = []

    def which(self, name):
        return f"/fake/bin/{name}" if name in self.tools else None

    def __call__(self, argv, input=None):
        self.calls.append(argv)
        if argv[:3] == ["gh", "auth", "status"]:
            body = "".join(f"github.com\n  \u2713 Logged in to github.com account {x} (keyring)\n  - Token: {SECRET}\n" for x in self.gh_accounts)
            body += "".join(f"github.com\n  X Failed to log in to github.com account {x} (keyring)\n  - The token in keyring is invalid.\n" for x in self.gh_failed)
            return (0 if self.gh_accounts and not self.gh_failed else 1), body
        if argv[:3] == ["claude", "auth", "status"]:
            return (0 if self.claude_ok else 1), json.dumps({"loggedIn": self.claude_ok, "apiKey": SECRET})
        if argv[:3] == ["codex", "login", "status"]:
            return (0 if self.codex_ok else 1), "Logged in using ChatGPT" if self.codex_ok else "Not logged in"
        if argv[0] == "claude" and "--help" in argv:
            return 0, "--bg  run in background"
        if argv[0] == "python3" and argv[1] == "-c":
            return 0, "True\n"
        if argv[0] == "git" and argv[1] == "config":
            return 0, "x\n"
        if argv[0] == "ssh-keygen":
            return (0, "256 SHA256:abc nat (ED25519)\n") if self.pub_ok and input else (1, "not a public key")
        return 0, f"{argv[0]} 1.0\n"


def make_env(tmp, fake, home_name="home"):
    home = Path(tmp) / home_name
    (home / "projects").mkdir(parents=True)
    # The wizard is run by the user whose HOME it checks: anything else is the --home override case (TargetEnvironmentTest).
    env = sw.Env(home=home, projects=home / "projects", runner=fake, which=fake.which, environ={"HOME": str(home)})
    return env


def write_key(env, name, body="PRIVATE", pub=True):
    ssh = env.home / ".ssh"
    ssh.mkdir(exist_ok=True)
    (ssh / name).write_text(body)
    if pub:
        (ssh / (name + ".pub")).write_text("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeFakeFakeFakeFakeFakeFakeFakeFakeFakeFake test\n")


def by_id(steps):
    return {s.id: s for s in steps}


class WizardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_bare_machine_everything_required_is_missing(self):
        env = make_env(self.tmp.name, Fake())
        steps = by_id(sw.build_plan(env, "generic"))
        for sid in ("tool-claude", "tool-codex", "tool-gh", "login-claude", "login-codex", "login-gh", "orch-home", "ssh-keys"):
            self.assertEqual(steps[sid].status, sw.MISSING, sid)
            self.assertTrue(steps[sid].detail)
        self.assertTrue(steps["tool-claude"].commands)

    @needs_toml
    def test_trust_missing_vs_pass_for_orch_and_each_repo(self):
        env = make_env(self.tmp.name, Fake(tools={"claude", "codex", "git", "python3", "gh"}))
        orch = env.orch_home
        (orch / ".codex").mkdir(parents=True)
        (orch / "AGENTS.md").write_text("x")
        (env.projects / "a" / ".git").mkdir(parents=True)
        (env.projects / "b" / ".git").mkdir(parents=True)
        (env.home / ".claude.json").write_text(json.dumps({"projects": {
            str(orch): {"hasTrustDialogAccepted": True}, str(env.projects / "a"): {"hasTrustDialogAccepted": True}}}))
        (env.home / ".codex").mkdir()
        (env.home / ".codex" / "config.toml").write_text(f'[projects."{orch}"]\ntrust_level = "trusted"\n')
        steps = by_id(sw.build_plan(env, "generic"))
        self.assertEqual(steps["trust-orch-claude"].status, sw.PASS)
        self.assertEqual(steps["trust-orch-codex"].status, sw.PASS)
        self.assertEqual(steps["trust-repo-a"].status, sw.PASS)
        self.assertEqual(steps["trust-repo-b"].status, sw.MISSING)
        self.assertIn("claude", steps["trust-repo-b"].commands[0])

    def test_unknown_when_state_files_unreadable_never_pass(self):
        env = make_env(self.tmp.name, Fake(tools={"claude"}))
        (env.orch_home).mkdir(parents=True)
        (env.orch_home / "AGENTS.md").write_text("x")
        (env.home / ".claude.json").write_text("{not json")
        steps = by_id(sw.build_plan(env, "generic"))
        self.assertEqual(steps["trust-orch-claude"].status, sw.UNKNOWN)
        self.assertEqual(steps["trust-orch-codex"].status, sw.UNKNOWN)
        self.assertEqual(steps["mcp-orchd-codex"].status, sw.UNKNOWN)
        self.assertEqual(steps["orch-config-paths"].status, sw.UNKNOWN)

    def test_login_failure_is_unknown_not_pass(self):
        env = make_env(self.tmp.name, Fake(tools={"claude", "codex"}, claude_ok=False, codex_ok=False))
        steps = by_id(sw.build_plan(env, "generic"))
        self.assertEqual(steps["login-claude"].status, sw.UNKNOWN)
        self.assertEqual(steps["login-codex"].status, sw.UNKNOWN)

    def test_doctor_is_never_pass_and_missing_doctor_is_said(self):
        env = make_env(self.tmp.name, Fake())
        step = by_id(sw.build_plan(env, "generic"))["doctor"]
        self.assertEqual(step.status, sw.UNKNOWN)
        self.assertEqual(step.status != sw.PASS, True)
        if not (env.checkout / "orchd" / "doctor.py").exists():
            self.assertIn("not in this checkout", step.detail)

    @needs_toml
    def test_other_machine_paths_in_orch_config_are_flagged(self):
        env = make_env(self.tmp.name, Fake())
        (env.orch_home / ".codex").mkdir(parents=True)
        (env.orch_home / "AGENTS.md").write_text("x")
        (env.orch_home / ".codex" / "config.toml").write_text(
            f'"/Users/olduser/projects/orch" = "write"\n"{env.home}/x" = "read"\n')
        step = by_id(sw.build_plan(env, "generic"))["orch-config-paths"]
        self.assertEqual(step.status, sw.MISSING)
        self.assertIn("/Users/olduser", step.detail)
        self.assertNotIn(str(env.home), step.detail.split("hardcodes")[1])

    def test_home_with_spaces_commands_are_quoted_and_portable(self):
        env = make_env(self.tmp.name, Fake(), home_name="my home/x y")
        text = sw.render(sw.build_plan(env, "all"), env)
        self.assertIn("'" + str(env.orch_home) + "'", text)  # shlex-quoted path with spaces
        self.assertIn(str(env.home), text)
        # Every home-relative path in the plan derives from this HOME (the checkout path is where the script lives).
        self.assertNotIn(str(Path.home() / ".ssh"), text)

    def test_other_machine_home_changes_every_path(self):
        a = make_env(self.tmp.name, Fake(), "homeA")
        b = make_env(self.tmp.name, Fake(), "homeB")
        ta, tb = sw.render(sw.build_plan(a, "all"), a), sw.render(sw.build_plan(b, "all"), b)
        self.assertNotIn("homeB", ta)
        self.assertNotIn("homeA", tb)

    def test_nat_profile_four_accounts_and_separation(self):
        env = make_env(self.tmp.name, Fake(tools={"gh"}, gh_accounts=["nat862", "NatChung"]))
        generic = sw.build_plan(env, "generic")
        nat = by_id(sw.build_plan(env, "nat"))
        self.assertFalse([s for s in generic if s.profile == "nat"])
        self.assertEqual({s.profile for s in nat.values()}, {"nat"})
        for acct in sw.NAT_ACCOUNTS:
            self.assertIn(f"gh-{acct}", nat)
        self.assertEqual(nat["gh-ariontechs"].status, sw.MISSING)
        self.assertIn("not logged in", nat["gh-ariontechs"].detail)
        for c in sw.NAT_CONNECTORS:
            self.assertEqual(nat[f"connector-{c}"].status, sw.MISSING)

    def test_nat_account_pass_needs_login_key_and_alias(self):
        env = make_env(self.tmp.name, Fake(tools={"gh"}, gh_accounts=["nat862"]))
        ssh = env.home / ".ssh"
        ssh.mkdir()
        write_key(env, "id_ed25519_nat862")
        (ssh / "config").write_text("Host github-nat862\n  HostName github.com\n")
        self.assertEqual(by_id(sw.build_plan(env, "nat"))["gh-nat862"].status, sw.PASS)
        (ssh / "config").write_text("")
        self.assertEqual(by_id(sw.build_plan(env, "nat"))["gh-nat862"].status, sw.MISSING)

    def test_connectors_present_are_unknown_not_pass(self):
        env = make_env(self.tmp.name, Fake())
        (env.projects / "nat-assistant" / "connectors" / "email-tools").mkdir(parents=True)
        step = by_id(sw.build_plan(env, "nat"))["connector-email-tools"]
        self.assertEqual(step.status, sw.UNKNOWN)
        self.assertIn("token presence NOT checked", step.detail)

    def test_zero_mutation_and_no_secret_leak(self):
        fake = Fake(tools={"claude", "codex", "gh", "git", "python3"}, gh_accounts=["nat862"])
        env = make_env(self.tmp.name, fake)
        (env.home / ".ssh").mkdir()
        (env.home / ".ssh" / "id_ed25519").write_text(f"PRIVATE-KEY-BODY {SECRET}")
        (env.home / ".claude.json").write_text(json.dumps({"apiKey": SECRET, "projects": {}}))
        (env.home / ".codex").mkdir()
        (env.home / ".codex" / "config.toml").write_text(f'token = "{SECRET}"\n')
        before = snapshot(env.home)
        steps = sw.build_plan(env, "all")
        text = sw.render(steps, env) + json.dumps([s.to_dict() for s in steps])
        self.assertEqual(snapshot(env.home), before)
        self.assertNotIn(SECRET, text)
        self.assertNotIn("PRIVATE-KEY-BODY", text)
        self.assert_only_allowlisted(env, fake)

    def assert_only_allowlisted(self, env, fake):
        """Exact argv match; no prefix or arity wildcards."""
        exact = set(sw.READ_ONLY_ARGV)
        for argv in fake.calls:
            self.assertIn(tuple(argv), exact, argv)

    def test_commands_are_printed_not_executed(self):
        fake = Fake()
        env = make_env(self.tmp.name, fake)
        sw.build_plan(env, "all")
        flat = [" ".join(a) for a in fake.calls]
        for bad in ("login --web", "auth login", "auth token", "ssh-keygen", "brew", "clone", "mcp add", "--help --bg"):
            self.assertFalse([c for c in flat if bad in c], bad)

    def test_plan_includes_receipt_checklist_and_issue_stays_open(self):
        env = make_env(self.tmp.name, Fake())
        text = sw.render(sw.build_plan(env, "generic"), env)
        for must in ("doctor", "ack", "progress", "report", "independent reviewer", "issue #3 stays"):
            self.assertIn(must, text)

    def test_required_step_coverage(self):
        env = make_env(self.tmp.name, Fake())
        ids = set(by_id(sw.build_plan(env, "all")))
        for need in ("tool-claude", "tool-codex", "tool-gh", "login-claude", "login-codex", "login-gh", "git-identity", "ssh-keys",
                     "orch-home", "trust-orch-claude", "trust-orch-codex", "orch-config-paths", "mcp-orchd-codex",
                     "orchd-home", "tmp-sockets", "doctor", "git-identity-per-repo"):
            self.assertIn(need, ids)

    def test_cli_exit_codes_and_json(self):
        env = make_env(self.tmp.name, Fake())
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(sw.main(["--json"], env=env), 0)
        self.assertIsInstance(json.loads(buf.getvalue()), list)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(sw.main(["--strict"], env=env), 1)

    def test_interactive_only_prints_and_rechecks(self):
        fake = Fake()
        env = make_env(self.tmp.name, fake)
        before = snapshot(env.home)
        lines = []
        answers = iter(["q"])
        sw.interactive(env, "generic", input_fn=lambda _: next(answers), out=lines.append)
        self.assertTrue(any("who:" in l for l in lines))
        self.assertEqual(snapshot(env.home), before)

    # -- rework of PR #28 review findings ------------------------------------------------------------------------
    def test_gh_rc1_invalid_token_is_not_pass(self):
        env = make_env(self.tmp.name, Fake(tools={"gh"}, gh_accounts=["nat862"], gh_failed=["NatChung"]))
        steps = by_id(sw.build_plan(env, "all"))
        self.assertNotEqual(steps["login-gh"].status, sw.PASS)
        self.assertEqual(steps["login-gh"].status, sw.UNKNOWN)
        self.assertIn("NatChung", steps["login-gh"].detail)
        self.assertEqual(steps["gh-NatChung"].status, sw.MISSING)
        self.assertIn("invalid", steps["gh-NatChung"].detail)

    def test_gh_rc1_with_logged_in_text_only_is_unknown_for_nat_accounts(self):
        fake = Fake(tools={"gh"}, gh_accounts=["nat862"], gh_failed=["x"])
        env = make_env(self.tmp.name, fake)
        write_key(env, "id_ed25519_nat862")
        (env.home / ".ssh" / "config").write_text("Host github-nat862\n")
        self.assertEqual(by_id(sw.build_plan(env, "nat"))["gh-nat862"].status, sw.UNKNOWN)

    def test_gh_not_logged_in_anywhere_is_missing(self):
        env = make_env(self.tmp.name, Fake(tools={"gh"}))
        self.assertEqual(by_id(sw.build_plan(env, "generic"))["login-gh"].status, sw.MISSING)

    def test_gh_all_valid_is_pass(self):
        env = make_env(self.tmp.name, Fake(tools={"gh"}, gh_accounts=["nat862", "NatChung"]))
        step = by_id(sw.build_plan(env, "generic"))["login-gh"]
        self.assertEqual(step.status, sw.PASS)
        self.assertNotIn(SECRET, step.detail)

    def test_claude_login_needs_logged_in_true_not_just_exit_0(self):
        class LoggedOutRc0(Fake):
            def __call__(self, argv, input=None):
                if argv[:3] == ["claude", "auth", "status"]:
                    self.calls.append(argv)
                    return 0, json.dumps({"loggedIn": False})
                return super().__call__(argv, input)
        env = make_env(self.tmp.name, LoggedOutRc0(tools={"claude"}))
        self.assertEqual(by_id(sw.build_plan(env, "generic"))["login-claude"].status, sw.UNKNOWN)

    def test_codex_not_logged_in_text_is_not_pass_even_rc0(self):
        class Rc0(Fake):
            def __call__(self, argv, input=None):
                if argv[:3] == ["codex", "login", "status"]:
                    self.calls.append(argv)
                    return 0, "Not logged in"
                return super().__call__(argv, input)
        env = make_env(self.tmp.name, Rc0(tools={"codex"}))
        self.assertEqual(by_id(sw.build_plan(env, "generic"))["login-codex"].status, sw.UNKNOWN)

    def test_credential_files_and_api_key_never_make_login_pass(self):
        fake = Fake(tools={"claude", "codex"}, claude_ok=False, codex_ok=False)
        env = make_env(self.tmp.name, fake)
        (env.home / ".claude").mkdir()
        (env.home / ".claude" / ".credentials.json").write_text(json.dumps({"accessToken": SECRET}))
        (env.home / ".codex").mkdir()
        (env.home / ".codex" / "auth.json").write_text(json.dumps({"OPENAI_API_KEY": SECRET}))
        (env.home / ".claude.json").write_text(json.dumps({"apiKey": SECRET, "projects": {}}))
        steps = by_id(sw.build_plan(env, "generic"))
        self.assertEqual(steps["login-claude"].status, sw.UNKNOWN)
        self.assertEqual(steps["login-codex"].status, sw.UNKNOWN)
        self.assertNotIn(SECRET, json.dumps([s.to_dict() for s in steps.values()]))

    def test_raw_credential_files_are_never_opened(self):
        env = make_env(self.tmp.name, Fake(tools={"claude", "codex", "gh"}, gh_accounts=["nat862"]))
        write_key(env, "id_ed25519_nat862", body=SECRET)
        (env.home / ".claude").mkdir()
        (env.home / ".claude" / ".credentials.json").write_text(SECRET)
        (env.home / ".codex").mkdir()
        (env.home / ".codex" / "auth.json").write_text(SECRET)
        opened = []
        real_open = Path.open
        real_rt, real_rb = Path.read_text, Path.read_bytes
        real_os_open = os.open

        def spy_os_open(path, *a, **k):
            opened.append(Path(os.fspath(path)).name)
            return real_os_open(path, *a, **k)

        def spy_open(self_, *a, **k):
            opened.append(self_.name)
            return real_open(self_, *a, **k)

        def spy_rt(self_, *a, **k):
            opened.append(self_.name)
            return real_rt(self_, *a, **k)

        def spy_rb(self_, *a, **k):
            opened.append(self_.name)
            return real_rb(self_, *a, **k)
        Path.open, Path.read_text, Path.read_bytes, sw.os.open = spy_open, spy_rt, spy_rb, spy_os_open
        try:
            sw.build_plan(env, "all")
        finally:
            Path.open, Path.read_text, Path.read_bytes, sw.os.open = real_open, real_rt, real_rb, real_os_open
        for name in (".credentials.json", "auth.json", "id_ed25519_nat862"):
            self.assertNotIn(name, opened)
        # Round 2: the .pub IS read now (public material, O_NOFOLLOW, format-checked) so ssh-keygen gets text, not a path.
        self.assertIn("id_ed25519_nat862.pub", opened)

    def test_not_a_key_file_is_not_pass(self):
        fake = Fake(tools={"gh"}, gh_accounts=["nat862"], pub_ok=False)
        env = make_env(self.tmp.name, fake)
        write_key(env, "id_ed25519_nat862", body="NOT A KEY")
        (env.home / ".ssh" / "config").write_text("Host github-nat862\n")
        steps = by_id(sw.build_plan(env, "all"))
        self.assertNotEqual(steps["gh-nat862"].status, sw.PASS)
        self.assertNotEqual(steps["ssh-keys"].status, sw.PASS)
        # private file alone, no public key to validate: unknown, not pass
        env2 = make_env(self.tmp.name, Fake(), "h2")
        write_key(env2, "id_ed25519", pub=False)
        self.assertEqual(by_id(sw.build_plan(env2, "generic"))["ssh-keys"].status, sw.UNKNOWN)

    def test_valid_key_pair_passes(self):
        env = make_env(self.tmp.name, Fake())
        write_key(env, "id_ed25519")
        self.assertEqual(by_id(sw.build_plan(env, "generic"))["ssh-keys"].status, sw.PASS)

    @needs_toml
    def test_home_with_space_own_paths_are_not_foreign(self):
        env = make_env(self.tmp.name, Fake(), home_name="Nat Space")
        (env.orch_home / ".codex").mkdir(parents=True)
        (env.orch_home / "AGENTS.md").write_text("x")
        cfg = env.orch_home / ".codex" / "config.toml"
        cfg.write_text(f'"{env.home}/projects/orch" = "write"\n"{env.home}" = "read"\n')
        step = by_id(sw.build_plan(env, "generic"))["orch-config-paths"]
        self.assertEqual(step.status, sw.PASS, step.detail)
        cfg.write_text(f'"{env.home}/x" = "write"\n"/Users/olduser/orch" = "read"\n')
        step = by_id(sw.build_plan(env, "generic"))["orch-config-paths"]
        self.assertEqual(step.status, sw.MISSING)
        self.assertIn("/Users/olduser", step.detail)
        self.assertNotIn("Nat Space", step.detail.split("hardcodes")[1])

    @needs_toml
    def test_home_like_users_nat_space_directly(self):
        env = make_env(self.tmp.name, Fake())
        env.home = Path("/Users/Nat Space")
        cfg = Path(self.tmp.name) / "c.toml"
        cfg.write_text('"/Users/Nat Space/projects" = "write"\n')
        self.assertEqual(sw.foreign_homes(env, cfg), [])
        cfg.write_text('"/Users/Nat Spacey/projects" = "write"\n')
        self.assertEqual(sw.foreign_homes(env, cfg), ["/Users/Nat Spacey"])

    def test_home_with_space_commands_in_plan_are_shell_safe(self):
        env = make_env(self.tmp.name, Fake(), home_name="Nat Space")
        text = sw.render(sw.build_plan(env, "all"), env)
        self.assertNotIn(f" -f {env.home}/", text)
        self.assertNotIn(f"IdentityFile {env.home}", text)

    def test_strict_interactive_quit_or_skip_with_open_steps_exits_1(self):
        import io, contextlib
        env = make_env(self.tmp.name, Fake())
        for answers in (["q"], ["s"] * 200):
            it = iter(answers)
            with contextlib.redirect_stdout(io.StringIO()):
                rc = sw.main(["--interactive", "--strict"], env=env, input_fn=lambda _: next(it))
            self.assertEqual(rc, 1, answers[:1])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(sw.main(["--interactive"], env=env, input_fn=lambda _: "q"), 0)

    def test_strict_interactive_eof_exits_1(self):
        import io, contextlib

        def eof(_):
            raise EOFError
        env = make_env(self.tmp.name, Fake())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(sw.main(["--interactive", "--strict"], env=env, input_fn=eof), 1)

    def test_run_refuses_everything_outside_the_exact_allowlist(self):
        fake = Fake(tools={"git", "gh", "claude", "codex"})
        env = make_env(self.tmp.name, fake)
        pub = str(env.home / ".ssh" / "id_x.pub")
        illegal = [
            ["git", "config", "--global", "user.name", "Mallory"], ["git", "config", "--global", "--unset", "user.name"],
            ["git", "config", "--global", "--get", "credential.helper"], ["git", "config", "user.name"],
            ["git", "config", "--global", "--add", "x.y", "z"], ["git", "config", "--global", "--get", "user.name", "--file", "/x"],
            ["gh", "auth", "login"], ["gh", "auth", "token"], ["gh", "auth", "status", "--show-token"], ["gh", "auth", "setup-git"],
            ["claude", "auth", "login"], ["claude", "mcp", "add", "x"], ["codex", "login"], ["codex", "login", "--api-key", "k"],
            ["gh", "repo", "clone", "x"], ["python3", "-c", "import os;os.remove('x')"], ["python3", "-m", "pip", "install", "x"],
            ["ssh-keygen", "-y", "-f", str(env.home / ".ssh" / "id_x")], ["ssh-keygen", "-l", "-f", str(env.home / ".ssh" / "id_x")],
            ["ssh-keygen", "-l", "-f", "/etc/ssh/ssh_host_rsa_key.pub"], ["ssh-keygen", "-l", "-f", pub + ".pub", "-x"],
            ["ssh-keygen", "-l", "-f", pub], ["ssh-keygen", "-l", "-f", "-", "-v"], ["ssh-keygen", "-l", "-f", "/dev/stdin"],
            ["ssh-keygen", "-t", "ed25519", "-f", pub], ["brew", "install", "gh"], ["xcode-select", "--install"],
            ["gh", "--version", "extra"], ["git", "--version", "--x"],
        ]
        for argv in illegal:
            with self.assertRaises(ValueError, msg=argv):
                env.run(argv)
        self.assertEqual(fake.calls, [])
        env.run(["ssh-keygen", "-l", "-f", "-"], input="ssh-ed25519 AAAA x\n")  # round 2: the one allowed shape, text on stdin
        env.run(["git", "config", "--global", "--get", "user.email"])

    def test_allowlist_is_pinned_to_this_exact_set(self):
        """Adding a command to the allowlist must be a deliberate edit of this list too."""
        self.assertEqual(sw.READ_ONLY_ARGV, {
            ("git", "--version"), ("python3", "--version"), ("claude", "--version"), ("codex", "--version"), ("gh", "--version"),
            ("python3", "-c", "import sys;print(sys.version_info >= (3, 11))"), ("claude", "--help"),
            ("claude", "auth", "status"), ("codex", "login", "status"), ("gh", "auth", "status"),
            ("git", "config", "--global", "--get", "user.name"), ("git", "config", "--global", "--get", "user.email"),
            ("ssh-keygen", "-l", "-f", "-"),
        })


def real_env(tmp, name, process_home=None, extra=None):
    """Env with the REAL runner (no Fake): only used for git and ssh-keygen on scratch files, never for live logins."""
    home = Path(tmp) / name
    home.mkdir(parents=True, exist_ok=True)
    environ = {"HOME": str(process_home or home), "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    environ.update(extra or {})
    return sw.Env(home=home, projects=home / "projects", environ=environ)


def gitconfig(home, email="t@example.com"):
    Path(home).mkdir(parents=True, exist_ok=True)
    (Path(home) / ".gitconfig").write_text(f"[user]\n\tname = T\n\temail = {email}\n")


def keypair(ssh_dir, name="id_ed25519"):
    """A throwaway Ed25519 pair made for the test in a scratch dir. Never ~/.ssh."""
    import subprocess
    ssh_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "scratch", "-f", str(ssh_dir / name)],
                   check=True, stdin=subprocess.DEVNULL, capture_output=True)
    return ssh_dir / name


class TargetEnvironmentTest(unittest.TestCase):
    """Round 2, finding 1: --home must not borrow the process HOME / GIT_CONFIG_* / provider overrides."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_real_git_actual_home_has_identity_target_empty_is_missing(self):
        actual = Path(self.tmp.name) / "actual"
        gitconfig(actual)
        env = real_env(self.tmp.name, "target", process_home=actual)
        self.assertEqual(sw.git_identity(env).status, sw.MISSING)

    def test_real_git_target_has_identity_actual_empty_is_pass(self):
        actual = Path(self.tmp.name) / "actual"
        actual.mkdir()
        env = real_env(self.tmp.name, "target", process_home=actual)
        gitconfig(env.home)
        self.assertEqual(sw.git_identity(env).status, sw.PASS)

    def test_real_git_same_home_both_ways(self):
        env = real_env(self.tmp.name, "h")
        self.assertEqual(sw.git_identity(env).status, sw.MISSING)
        gitconfig(env.home)
        self.assertEqual(sw.git_identity(env).status, sw.PASS)

    def test_real_git_config_overrides_are_unknown_not_a_verdict(self):
        other = Path(self.tmp.name) / "other"
        gitconfig(other)
        for extra in ({"GIT_CONFIG_GLOBAL": str(other / ".gitconfig")}, {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "user.email",
                      "GIT_CONFIG_VALUE_0": "x@y"}, {"XDG_CONFIG_HOME": str(other)}, {"GIT_CONFIG_PARAMETERS": "'user.name'='x'"}):
            for target_has in (False, True):
                env = real_env(self.tmp.name, f"t{len(extra)}{target_has}", extra=extra)
                if target_has:
                    gitconfig(env.home)
                step = sw.git_identity(env)
                self.assertEqual(step.status, sw.UNKNOWN, (extra, target_has))
                for k in extra:
                    if k.startswith(("GIT_CONFIG_GLOBAL", "XDG", "GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS")):
                        self.assertIn(k, step.detail)
                self.assertNotIn("x@y", step.detail)
                self.assertNotIn(str(other), step.detail)

    def test_home_override_makes_live_logins_unknown_even_if_process_user_is_logged_in(self):
        fake = Fake(tools={"claude", "codex", "gh"}, gh_accounts=["nat862", "NatChung"])
        env = make_env(self.tmp.name, fake)
        env.environ = {"HOME": str(Path(self.tmp.name) / "someone-else")}
        write_key(env, "id_ed25519_nat862")
        (env.home / ".ssh" / "config").write_text("Host github-nat862\n")
        steps = by_id(sw.build_plan(env, "all"))
        for sid in ("login-claude", "login-codex", "login-gh", "gh-nat862"):
            self.assertEqual(steps[sid].status, sw.UNKNOWN, sid)
            self.assertIn("HOME", steps[sid].detail, sid)
        # NatChung has no key in this HOME: missing for that reason, and its gh login is still not claimed either way.
        self.assertEqual(steps["gh-NatChung"].status, sw.MISSING)
        self.assertIn("gh login for NatChung not checked", steps["gh-NatChung"].detail)
        auth = [a for a in fake.calls if a[1:2] in (["auth"], ["login"])]
        self.assertEqual(auth, [])  # not even asked: the answer would describe the wrong user

    def test_home_override_real_runner_never_writes_into_target_home(self):
        """gh and codex create dirs under $HOME on startup. A stub that does the same must not touch --home."""
        bin_dir = Path(self.tmp.name) / "bin"
        bin_dir.mkdir()
        for name in ("gh", "codex", "claude"):
            stub = bin_dir / name
            stub.write_text('#!/bin/sh\nmkdir -p "$HOME/.stub-state"\necho "stub 1.0"\n')
            stub.chmod(0o755)
        actual = Path(self.tmp.name) / "actual"
        actual.mkdir()
        env = real_env(self.tmp.name, "target", process_home=actual)
        env.environ["PATH"] = f"{bin_dir}:{env.environ['PATH']}"
        env.which = lambda n: shutil.which(n, path=env.environ["PATH"])
        gitconfig(env.home)
        before = snapshot(env.home)
        steps = by_id(sw.build_plan(env, "generic"))
        self.assertEqual(snapshot(env.home), before)
        self.assertEqual(steps["git-identity"].status, sw.PASS)  # git alone reads the target HOME
        self.assertEqual(steps["login-gh"].status, sw.UNKNOWN)

    def test_home_override_missing_key_is_still_missing(self):
        env = make_env(self.tmp.name, Fake(tools={"gh"}, gh_accounts=["nat862"]))
        env.environ = {"HOME": "/nonexistent-process-home"}
        self.assertEqual(by_id(sw.build_plan(env, "nat"))["gh-nat862"].status, sw.MISSING)

    def test_provider_overrides_make_that_provider_unknown_names_only(self):
        cases = {
            "login-gh": ["GH_TOKEN", "GITHUB_TOKEN", "GH_CONFIG_DIR", "GH_ENTERPRISE_TOKEN", "GH_HOST", "XDG_CONFIG_HOME"],
            "login-claude": ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR"],
            "login-codex": ["OPENAI_API_KEY", "CODEX_HOME"],
        }
        for sid, names in cases.items():
            for name in names:
                fake = Fake(tools={"claude", "codex", "gh"}, gh_accounts=["nat862"])
                env = make_env(self.tmp.name, fake, home_name=f"{sid}-{name}")
                env.environ[name] = SECRET
                step = by_id(sw.build_plan(env, "generic"))[sid]
                self.assertEqual(step.status, sw.UNKNOWN, (sid, name))
                self.assertIn(name, step.detail)
                self.assertNotIn(SECRET, step.detail)

    def test_config_dir_overrides_make_trust_and_mcp_file_checks_unknown(self):
        env = make_env(self.tmp.name, Fake(tools={"claude", "codex"}))
        orch = env.orch_home
        orch.mkdir(parents=True)
        (orch / "AGENTS.md").write_text("x")
        (env.home / ".claude.json").write_text(json.dumps({"projects": {str(orch): {"hasTrustDialogAccepted": True}}}))
        (env.home / ".codex").mkdir()
        (env.home / ".codex" / "config.toml").write_text(f'[projects."{orch}"]\ntrust_level = "trusted"\n[mcp_servers.orchd]\ncommand = "x"\n')
        env.environ.update({"CLAUDE_CONFIG_DIR": "/elsewhere", "CODEX_HOME": "/elsewhere2"})
        steps = by_id(sw.build_plan(env, "generic"))
        for sid in ("trust-orch-claude", "trust-orch-codex", "mcp-orchd-codex"):
            self.assertEqual(steps[sid].status, sw.UNKNOWN, sid)

    def test_matching_home_without_overrides_still_passes(self):
        env = make_env(self.tmp.name, Fake(tools={"claude", "codex", "gh"}, gh_accounts=["nat862"]))
        steps = by_id(sw.build_plan(env, "generic"))
        for sid in ("login-claude", "login-codex", "login-gh"):
            self.assertEqual(steps[sid].status, sw.PASS, sid)


class PublicKeyOnlyTest(unittest.TestCase):
    """Round 2, finding 2: real disposable keys, real ssh-keygen. The .pub must be a public key, not private material."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = real_env(self.tmp.name, "h")
        self.key = keypair(self.env.home / ".ssh")
        self.pub = Path(str(self.key) + ".pub")

    def test_real_pair_passes(self):
        self.assertTrue(sw.pub_key_ok(self.env, self.key))

    def test_pub_symlink_to_private_is_refused(self):
        self.pub.unlink()
        self.pub.symlink_to(self.key)
        self.assertFalse(sw.pub_key_ok(self.env, self.key))

    def test_pub_symlink_to_a_real_public_key_is_refused_too(self):
        elsewhere = keypair(Path(self.tmp.name) / "other")
        self.pub.unlink()
        self.pub.symlink_to(str(elsewhere) + ".pub")
        self.assertFalse(sw.pub_key_ok(self.env, self.key))

    def test_pub_with_private_key_copy_is_refused(self):
        self.pub.write_bytes(self.key.read_bytes())
        self.assertFalse(sw.pub_key_ok(self.env, self.key))

    def test_pub_hardlinked_to_private_is_refused(self):
        self.pub.unlink()
        os.link(self.key, self.pub)
        self.assertFalse(sw.pub_key_ok(self.env, self.key))

    def test_garbage_and_non_regular_pub_are_refused(self):
        self.pub.write_text("ssh-ed25519 not-base64!!\n")
        self.assertFalse(sw.pub_key_ok(self.env, self.key))
        self.pub.write_text("ssh-ed25519 AAAA test")  # well-formed shape but not a key: ssh-keygen says no
        self.assertFalse(sw.pub_key_ok(self.env, self.key))
        self.pub.unlink()
        os.mkfifo(self.pub)
        self.assertFalse(sw.pub_key_ok(self.env, self.key))  # must not block on a FIFO

    def test_private_key_is_never_opened_and_ssh_keygen_never_sees_a_path(self):
        opened, argvs = [], []
        real_os_open = os.open

        def spy(path, *a, **k):
            opened.append(os.fspath(path))
            return real_os_open(path, *a, **k)
        real_run = self.env.run

        def run_spy(argv, **k):
            argvs.append(list(argv))
            return real_run(argv, **k)
        self.env.run = run_spy
        os.open = spy
        try:
            self.assertTrue(sw.pub_key_ok(self.env, self.key))
        finally:
            os.open = real_os_open
        self.assertNotIn(str(self.key), opened)
        self.assertEqual(argvs, [["ssh-keygen", "-l", "-f", "-"]])

    def test_ssh_keys_step_with_private_copy_as_pub_is_not_pass(self):
        self.pub.write_bytes(self.key.read_bytes())
        steps = by_id(sw.build_plan(sw.Env(home=self.env.home, projects=self.env.home / "projects",
                                           runner=lambda argv, **k: (1, ""), which=lambda _: None, environ=self.env.environ), "generic"))
        self.assertNotEqual(steps["ssh-keys"].status, sw.PASS)


class HomeBoundaryTest(unittest.TestCase):
    """Round 2, finding 3: HOME is a whole path component, never a prefix that swallows '/Users/Nat Space'."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Path(self.tmp.name) / "c.toml"

    def scan(self, home, text):
        env = make_env(self.tmp.name, Fake(), home_name=f"x{len(list(Path(self.tmp.name).iterdir()))}")
        env.home = Path(home)
        self.cfg.write_text(text)
        return sw.foreign_homes(env, self.cfg)

    @needs_toml
    def test_reviewer_repro_quoted_command_with_space(self):
        text = "[mcp_servers.orchd]\ncommand='/Users/Nat Space/projects/orchd/bin/orchd'\n"
        self.assertEqual(self.scan("/Users/Nat", text), ["/Users/Nat Space"])
        self.assertEqual(self.scan("/Users/Nat Space", text), [])

    @needs_toml
    def test_prefix_overlap_and_linux_homes_with_spaces(self):
        self.assertEqual(self.scan("/Users/Nat", '"/Users/Natalie/x" = 1\n'), ["/Users/Natalie"])
        self.assertEqual(self.scan("/Users/Nat", '"/Users/Nat/x" = 1\nk = "/Users/Nat"\n'), [])
        self.assertEqual(self.scan("/home/a b", 'a = ["/home/a b/x", "/home/a bc/y", "/home/a"]\n'), ["/home/a", "/home/a bc"])
        self.assertEqual(self.scan("/home/a", 'p = "/home/a b/x"\n'), ["/home/a b"])

    def test_unparseable_toml_is_unknown_not_pass(self):
        self.assertIsNone(self.scan("/Users/Nat", "this is = = not toml '/Users/old/x'\n"))


class CodexStatusShapeTest(unittest.TestCase):
    """Round 2, finding 4: only the affirmative shapes codex prints count as logged in."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def status(self, rc, text, name):
        class F(Fake):
            def __call__(self, argv, **k):
                if argv[:3] == ["codex", "login", "status"]:
                    return rc, text
                return super().__call__(argv, **k)
        env = make_env(self.tmp.name, F(tools={"codex"}), home_name=name)
        return by_id(sw.build_plan(env, "generic"))["login-codex"].status

    def test_affirmative_shapes_pass(self):
        self.assertEqual(self.status(0, "Logged in using ChatGPT\n", "a"), sw.PASS)
        self.assertEqual(self.status(0, "Logged in using an API key - sk-proj-***ABCD\n", "b"), sw.PASS)

    def test_anything_else_with_rc0_is_unknown(self):
        for i, text in enumerate(("Unable to determine whether you are logged in", "Error checking login status: logged in?",
                                  "You are not logged in", "warning: x\nLogged in using ChatGPT", "logged in", "",
                                  "Logged in using a token from somewhere", "Not logged in")):
            self.assertEqual(self.status(0, text, f"u{i}"), sw.UNKNOWN, text)
        self.assertEqual(self.status(1, "Logged in using ChatGPT", "rc1"), sw.UNKNOWN)


class OrchdPathOverrideTest(unittest.TestCase):
    """Round 3: the caller's ORCHD_ORCH_HOME / ORCHD_HOME under a foreign --home must not credit the caller's state to
    the target. Caller side is fully set up (orch with AGENTS.md and config, writable state, target trusting the caller
    orch) so every negative case would pass if the override leaked."""

    ORCH_STEPS = ("orch-home", "trust-orch-claude", "trust-orch-codex", "orch-config-paths")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.caller, self.target = root / "caller", root / "target"
        for d in (self.caller / "orch" / ".codex", self.caller / "state", self.target / "projects"):
            d.mkdir(parents=True)
        (self.caller / "orch" / "AGENTS.md").write_text("x")
        (self.caller / "orch" / ".codex" / "config.toml").write_text(f'[mcp_servers.orchd]\ncommand = "{self.caller}/x"\n')
        trust = {str(self.caller / "orch"): {"hasTrustDialogAccepted": True}}
        (self.target / ".claude.json").write_text(json.dumps({"projects": trust}))
        (self.target / ".codex").mkdir()
        (self.target / ".codex" / "config.toml").write_text(f'[projects."{self.caller / "orch"}"]\ntrust_level = "trusted"\n')
        self.ro = self.target / ".local"
        self.ro.mkdir()
        self.ro.chmod(0o500)  # target state parent not writable: a real probe of the target says missing
        self.addCleanup(self.ro.chmod, 0o700)

    def foreign(self, **overrides):
        env = sw.Env(home=self.target, projects=self.target / "projects", runner=Fake(), which=lambda n: None,
                     environ={"HOME": str(self.caller), **{k: str(v) for k, v in overrides.items()}})
        return by_id(sw.build_plan(env, "generic"))

    def assert_unknown_names_only(self, steps, sids, name):
        for sid in sids:
            st = steps[sid]
            self.assertEqual(st.status, sw.UNKNOWN, sid)
            self.assertIn(name, st.detail, sid)
            self.assertIn(name, st.source, sid)  # no claim that a file was read or scanned
            for text in (st.detail, st.source, st.receipt, *st.commands):
                self.assertNotIn(str(self.caller), text, sid)

    def test_foreign_home_orch_home_override_alone(self):
        steps = self.foreign(ORCHD_ORCH_HOME=self.caller / "orch")
        self.assert_unknown_names_only(steps, self.ORCH_STEPS, "ORCHD_ORCH_HOME")
        self.assertEqual(steps["orchd-home"].status, sw.MISSING)  # unaffected: still probes the target default

    def test_foreign_home_orchd_home_override_alone(self):
        steps = self.foreign(ORCHD_HOME=self.caller / "state")
        self.assert_unknown_names_only(steps, ("orchd-home",), "ORCHD_HOME")
        self.assertEqual(steps["orch-home"].status, sw.MISSING)  # target default has no Orch home
        self.assertIn(str(self.target / "orch" / "home"), steps["orch-home"].detail)
        self.assertEqual(steps["tmp-sockets"].status, sw.PASS)

    def test_foreign_home_both_overrides(self):
        steps = self.foreign(ORCHD_ORCH_HOME=self.caller / "orch", ORCHD_HOME=self.caller / "state")
        self.assert_unknown_names_only(steps, self.ORCH_STEPS, "ORCHD_ORCH_HOME")
        self.assert_unknown_names_only(steps, ("orchd-home",), "ORCHD_HOME")
        self.assertNotIn(str(self.caller), sw.render(list(steps.values()), sw.Env(home=self.target, projects=self.target)))

    def test_foreign_home_without_overrides_checks_target_defaults(self):
        steps = self.foreign()
        self.assertEqual(steps["orch-home"].status, sw.MISSING)
        self.assertEqual(steps["orchd-home"].status, sw.MISSING)

    def same(self, **overrides):
        env = sw.Env(home=self.target, projects=self.target / "projects", runner=Fake(), which=lambda n: None,
                     environ={"HOME": str(self.target), **{k: str(v) for k, v in overrides.items()}})
        return by_id(sw.build_plan(env, "generic"))

    @needs_toml
    def test_same_home_orch_home_override_is_honoured(self):
        steps = self.same(ORCHD_ORCH_HOME=self.caller / "orch")
        self.assertEqual(steps["orch-home"].status, sw.PASS)
        self.assertEqual(steps["trust-orch-claude"].status, sw.PASS)
        self.assertEqual(steps["trust-orch-codex"].status, sw.PASS)
        self.assertIn(str(self.caller / "orch" / ".codex" / "config.toml"), steps["orch-config-paths"].detail)  # scans the override
        self.assertEqual(steps["orchd-home"].status, sw.MISSING)

    def test_same_home_orchd_home_override_is_honoured(self):
        steps = self.same(ORCHD_HOME=self.caller / "state")
        self.assertEqual(steps["orchd-home"].status, sw.PASS)
        self.assertEqual(steps["orch-home"].status, sw.MISSING)

    def test_same_home_both_overrides_are_honoured(self):
        steps = self.same(ORCHD_ORCH_HOME=self.caller / "orch", ORCHD_HOME=self.caller / "state")
        self.assertEqual(steps["orch-home"].status, sw.PASS)
        self.assertEqual(steps["orchd-home"].status, sw.PASS)

if __name__ == "__main__":
    unittest.main()


class NoTomllibTest(unittest.TestCase):
    """Python 3.9/3.10 has no tomllib. TOML-backed checks must say unknown (never pass/missing, never crash) and the
    non-TOML checks must still run. Forced here so it is judged on every interpreter."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        real = sw.tomllib
        sw.tomllib = None
        self.addCleanup(setattr, sw, "tomllib", real)

    def test_toml_checks_unknown_even_when_file_would_say_pass_or_missing(self):
        env = make_env(self.tmp.name, Fake(tools={"git", "python3"}))
        (env.orch_home / ".codex").mkdir(parents=True)
        (env.orch_home / "AGENTS.md").write_text("x")
        (env.orch_home / ".codex" / "config.toml").write_text(f'"/Users/olduser/x" = "write"\n[projects."{env.orch_home}"]\ntrust_level = "trusted"\n')
        (env.home / ".codex").mkdir()
        (env.home / ".codex" / "config.toml").write_text(f'[projects."{env.orch_home}"]\ntrust_level = "trusted"\n[mcp_servers.orchd]\ncommand = "x"\n')
        steps = by_id(sw.build_plan(env, "generic"))
        for sid in ("trust-orch-codex", "orch-config-paths", "mcp-orchd-codex"):
            self.assertEqual(steps[sid].status, sw.UNKNOWN, sid)
            self.assertIn("tomllib", steps[sid].detail, sid)
        self.assertEqual(steps["tool-git"].status, sw.PASS)  # non-TOML checks still run
        self.assertIn(steps["orch-home"].status, (sw.PASS, sw.MISSING))

    def test_read_toml_returns_none_without_parsing(self):
        p = Path(self.tmp.name) / "c.toml"
        p.write_text("a = 1\n")
        self.assertIsNone(sw.read_toml(p))

    def test_json_checks_unaffected(self):
        env = make_env(self.tmp.name, Fake(tools={"claude"}))
        (env.home / ".claude.json").write_text(json.dumps({"projects": {str(env.orch_home): {"hasTrustDialogAccepted": True}}}))
        self.assertEqual(sw.claude_trusted(env, env.orch_home)[0], sw.PASS)


class ImportWithoutTomllibTest(unittest.TestCase):
    def test_module_imports_and_runs_help_when_tomllib_is_blocked(self):
        import subprocess, sys
        script = Path(__file__).resolve().parents[1] / "scripts" / "setup-wizard.py"
        code = ("import sys; sys.modules['tomllib'] = None; import runpy; sys.argv = [%r, '--help']; "
                "runpy.run_path(%r, run_name='__main__')" % (str(script), str(script)))
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        self.assertIn("usage:", r.stdout)
        self.assertNotIn("ImportError", r.stderr)
