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
    """Runner + which for a machine that has the listed tools."""

    def __init__(self, tools=(), gh_accounts=(), claude_ok=True, codex_ok=True):
        self.tools, self.gh_accounts, self.claude_ok, self.codex_ok = set(tools), gh_accounts, claude_ok, codex_ok
        self.calls = []

    def which(self, name):
        return f"/fake/bin/{name}" if name in self.tools else None

    def __call__(self, argv):
        self.calls.append(argv)
        a = argv[:3]
        if argv[0] == "gh" and argv[1:3] == ["auth", "status"]:
            body = "".join(f"github.com\n  ✓ Logged in to github.com account {x} (keyring)\n  - Token: {SECRET}\n" for x in self.gh_accounts)
            return (0 if self.gh_accounts else 1), body
        if argv[0] == "claude" and argv[1] == "auth":
            return (0 if self.claude_ok else 1), f"token {SECRET}"
        if argv[0] == "codex" and argv[1] == "login":
            return (0 if self.codex_ok else 1), ""
        if argv[0] == "claude" and "--help" in argv:
            return 0, "--bg  run in background"
        if argv[0] == "python3" and argv[1] == "-c":
            return 0, "True\n"
        if argv[0] == "git" and argv[1] == "config":
            return 0, "x\n"
        return 0, f"{argv[0]} 1.0\n"


def make_env(tmp, fake, home_name="home"):
    home = Path(tmp) / home_name
    (home / "projects").mkdir(parents=True)
    env = sw.Env(home=home, projects=home / "projects", runner=fake, which=fake.which, environ={})
    return env


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
        (ssh / "id_ed25519_nat862").write_text("PRIVATE")
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
        # Only read-only subcommands were ever run.
        for argv in fake.calls:
            self.assertIn(tuple(argv[:3]) if argv[0] != "git" else tuple(argv[:2]), {
                ("gh", "auth", "status"), ("claude", "auth", "status"), ("codex", "login", "status"),
                ("claude", "--help"), ("python3", "-c", "import sys;print(sys.version_info >= (3, 11))"),
                ("git", "config"), ("claude", "--version"), ("codex", "--version"), ("gh", "--version"),
                ("git", "--version"), ("python3", "--version"), ("gh", "--version")} | {(a[0], a[1]) for a in fake.calls if len(a) == 2})

    def test_commands_are_printed_not_executed(self):
        fake = Fake()
        env = make_env(self.tmp.name, fake)
        sw.build_plan(env, "all")
        flat = [" ".join(a) for a in fake.calls]
        for bad in ("login --web", "ssh-keygen", "brew", "clone", "mcp add", "--help --bg"):
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


if __name__ == "__main__":
    unittest.main()
