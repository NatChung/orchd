"""setup-wizard: plan content, zero mutation, HOME portability, no token leaks. Everything runs on a scratch HOME
with a fake runner; nothing real is installed, logged in or trusted."""
import hashlib
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("setup_wizard", Path(__file__).resolve().parents[1] / "scripts" / "setup-wizard.py")
sw = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sw)

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

    def __call__(self, argv):
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
            return (0, "256 SHA256:abc nat (ED25519)\n") if self.pub_ok else (1, "not a public key")
        return 0, f"{argv[0]} 1.0\n"


def make_env(tmp, fake, home_name="home"):
    home = Path(tmp) / home_name
    (home / "projects").mkdir(parents=True)
    env = sw.Env(home=home, projects=home / "projects", runner=fake, which=fake.which, environ={})
    return env


def write_key(env, name, body="PRIVATE", pub=True):
    ssh = env.home / ".ssh"
    ssh.mkdir(exist_ok=True)
    (ssh / name).write_text(body)
    if pub:
        (ssh / (name + ".pub")).write_text("ssh-ed25519 AAAA test")


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
            ok = tuple(argv) in exact or (len(argv) == 4 and argv[:3] == ["ssh-keygen", "-l", "-f"]
                                          and argv[3] == str(env.home / ".ssh" / Path(argv[3]).name) and argv[3].endswith(".pub"))
            self.assertTrue(ok, argv)

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
            def __call__(self, argv):
                if argv[:3] == ["claude", "auth", "status"]:
                    self.calls.append(argv)
                    return 0, json.dumps({"loggedIn": False})
                return super().__call__(argv)
        env = make_env(self.tmp.name, LoggedOutRc0(tools={"claude"}))
        self.assertEqual(by_id(sw.build_plan(env, "generic"))["login-claude"].status, sw.UNKNOWN)

    def test_codex_not_logged_in_text_is_not_pass_even_rc0(self):
        class Rc0(Fake):
            def __call__(self, argv):
                if argv[:3] == ["codex", "login", "status"]:
                    self.calls.append(argv)
                    return 0, "Not logged in"
                return super().__call__(argv)
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

        def spy_open(self_, *a, **k):
            opened.append(self_.name)
            return real_open(self_, *a, **k)

        def spy_rt(self_, *a, **k):
            opened.append(self_.name)
            return real_rt(self_, *a, **k)

        def spy_rb(self_, *a, **k):
            opened.append(self_.name)
            return real_rb(self_, *a, **k)
        Path.open, Path.read_text, Path.read_bytes = spy_open, spy_rt, spy_rb
        try:
            sw.build_plan(env, "all")
        finally:
            Path.open, Path.read_text, Path.read_bytes = real_open, real_rt, real_rb
        for name in (".credentials.json", "auth.json", "id_ed25519_nat862", "id_ed25519_nat862.pub"):
            self.assertNotIn(name, opened)

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
            ["ssh-keygen", "-t", "ed25519", "-f", pub], ["brew", "install", "gh"], ["xcode-select", "--install"],
            ["gh", "--version", "extra"], ["git", "--version", "--x"],
        ]
        for argv in illegal:
            with self.assertRaises(ValueError, msg=argv):
                env.run(argv)
        self.assertEqual(fake.calls, [])
        env.run(["ssh-keygen", "-l", "-f", pub])  # the one allowed shape
        env.run(["git", "config", "--global", "--get", "user.email"])

    def test_allowlist_is_pinned_to_this_exact_set(self):
        """Adding a command to the allowlist must be a deliberate edit of this list too."""
        self.assertEqual(sw.READ_ONLY_ARGV, {
            ("git", "--version"), ("python3", "--version"), ("claude", "--version"), ("codex", "--version"), ("gh", "--version"),
            ("python3", "-c", "import sys;print(sys.version_info >= (3, 11))"), ("claude", "--help"),
            ("claude", "auth", "status"), ("codex", "login", "status"), ("gh", "auth", "status"),
            ("git", "config", "--global", "--get", "user.name"), ("git", "config", "--global", "--get", "user.email"),
        })

if __name__ == "__main__":
    unittest.main()
