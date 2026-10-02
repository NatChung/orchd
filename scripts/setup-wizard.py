#!/usr/bin/env python3
"""Setup wizard for moving orchd to a new machine (issue #3, part B).

Read-only. It checks the machine, prints the steps that are still open, says who runs each command and which
receipt proves it worked. It never installs, logs in, trusts, writes config, copies tokens or runs the commands it
prints. Everything is computed from the current HOME, so nothing is tied to one machine's paths.

  setup-wizard.py [--profile generic|nat|all] [--home DIR] [--projects DIR] [--json] [--interactive] [--strict]

Exit code: 0 once the plan is printed; with --strict, 1 unless every step is `pass`.
"""
import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

PASS, MISSING, UNKNOWN, MANUAL = "pass", "missing", "unknown", "manual"

# Nat's accounts: gh account -> (ssh host alias, key file, remote host form). Only used by the `nat` profile.
NAT_ACCOUNTS = {
    "ariontechs": (None, "id_ed25519"),
    "NatChung": ("github-NatChung", "id_ed25519_NatChung"),
    "nat862": ("github-nat862", "id_ed25519_nat862"),
    "natchung-gif": ("github-natchung-kc", "id_ed25519_natchung_gif"),
}
NAT_CONNECTORS = ("email-tools", "slack-tools", "line-tools")
NAT_CLIS = ("codegraph", "rtk", "gcloud", "fastlane")
PY_VERSION_CHECK = "import sys;print(sys.version_info >= (3, 11))"
READ_ONLY_ARGV = {
    ("git", "--version"), ("python3", "--version"), ("claude", "--version"), ("codex", "--version"), ("gh", "--version"),
    ("python3", "-c", PY_VERSION_CHECK), ("claude", "--help"),
    ("claude", "auth", "status"), ("codex", "login", "status"), ("gh", "auth", "status"),
    ("git", "config", "--global", "--get", "user.name"), ("git", "config", "--global", "--get", "user.email"),
}
MIN_NOTE = "orchd needs `claude --bg` and the hidden `--messaging-socket-path` flag (Claude Code 2.1.284 was tested)"


@dataclass
class Env:
    home: Path
    projects: Path
    runner: object = None  # callable(argv) -> (returncode, stdout); None = real subprocess
    which: object = shutil.which
    environ: dict = field(default_factory=lambda: dict(os.environ))

    def allowed(self, argv):
        """Fixed read-only argv allowlist. Anything else (logins, config writes, key reads) is refused."""
        a = tuple(argv)
        if a in READ_ONLY_ARGV:
            return True
        if len(a) == 4 and a[:3] == ("ssh-keygen", "-l", "-f"):
            pub = Path(a[3])  # fingerprint of a PUBLIC key file only; the private key is never opened
            return pub.suffix == ".pub" and pub.parent == self.home / ".ssh"
        return False

    def run(self, argv, timeout=20):
        if not self.allowed(argv):
            raise ValueError(f"refusing non-allowlisted command: {argv!r}")
        if self.runner:
            return self.runner(argv)
        try:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
            return p.returncode, p.stdout + p.stderr
        except (OSError, subprocess.SubprocessError):
            return 127, ""

    @property
    def orch_home(self):
        return Path(self.environ.get("ORCHD_ORCH_HOME") or self.home / "projects" / "orch")

    @property
    def orchd_home(self):
        return Path(self.environ.get("ORCHD_HOME") or self.home / ".local/share/orchd")

    @property
    def checkout(self):
        return Path(__file__).resolve().parents[1]


@dataclass
class Step:
    id: str
    profile: str
    title: str
    who: str
    status: str
    detail: str
    source: str
    commands: list = field(default_factory=list)
    receipt: str = ""

    def to_dict(self):
        return self.__dict__.copy()


def q(path):
    return shlex.quote(str(path))


def read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def read_toml(path):
    try:
        return tomllib.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def claude_trusted(env, path):
    # ~/.claude.json can also hold an apiKey. Keep only the one trust boolean; every other key is dropped at once
    # and never used for a verdict or shown.
    data = read_json(env.home / ".claude.json")
    if data is None:
        return UNKNOWN, f"{env.home}/.claude.json unreadable or missing (nothing trusted yet, or file not parseable)"
    ok = bool(((data.get("projects") or {}).get(str(path)) or {}).get("hasTrustDialogAccepted") is True)
    del data
    return (PASS, "trusted") if ok else (MISSING, "not trusted")


def codex_trusted(env, path):
    data = read_toml(env.home / ".codex" / "config.toml")
    if data is None:
        return UNKNOWN, f"{env.home}/.codex/config.toml unreadable or missing"
    level = ((data.get("projects") or {}).get(str(path)) or {}).get("trust_level")
    del data
    return (PASS, "trusted") if level == "trusted" else (MISSING, "not trusted")


def tool_step(env, name, profile, who, title, commands, receipt, version_args=("--version",)):
    path = env.which(name)
    if not path:
        return Step(f"tool-{name}", profile, title, who, MISSING, f"`{name}` not on PATH", "PATH lookup", commands, receipt)
    rc, out = env.run([name, *version_args])
    line = out.strip().splitlines()[0][:80] if rc == 0 and out.strip() else ""
    return Step(f"tool-{name}", profile, title, who, PASS if rc == 0 else UNKNOWN,
                f"found at {path}" + (f", {line}" if line else "") if rc == 0 else f"found at {path} but `{name} {' '.join(version_args)}` failed",
                "PATH lookup + version", commands, receipt)


def gh_state(env):
    """(rc, logged_in, failed) from `gh auth status`; None if gh is missing. Only account names are kept, so masked or
    real tokens cannot leak. `Failed to log in ... account X` lines (invalid token) are not logged-in accounts, and a
    non-zero exit means gh could not confirm every account, so callers must not report pass."""
    if not env.which("gh"):
        return None
    rc, out = env.run(["gh", "auth", "status"])
    ok = sorted(set(re.findall(r"Logged in to \S+ account (\S+)", out)))
    bad = sorted(set(re.findall(r"Failed to log in to \S+ account (\S+)", out)))
    return rc, ok, bad


def gh_login_ok(state, acct=None):
    """True only when gh exits 0 (every stored token verified) and the account is listed as logged in."""
    if state is None or state[0] != 0:
        return False
    return bool(state[1]) if acct is None else acct in state[1]


def pub_key_ok(env, key):
    """The private key file must exist (stat only) and its .pub must parse as a public key. The private file is
    never opened, so a file of garbage cannot pass by existing and a real key is never read."""
    pub = Path(str(key) + ".pub")
    if not key.is_file() or not pub.is_file():
        return False
    try:
        rc, _ = env.run(["ssh-keygen", "-l", "-f", str(pub)])
    except ValueError:
        return False
    return rc == 0


def ssh_hosts(env):
    try:
        text = (env.home / ".ssh" / "config").read_text()
    except OSError:
        return None
    hosts = set()
    for m in re.finditer(r"^\s*Host\s+(.+)$", text, re.M):
        hosts.update(m.group(1).split())
    return hosts


def foreign_homes(env, path):
    """Absolute /Users/<x> or /home/<x> paths in a config that do not belong to this machine's HOME."""
    try:
        text = Path(path).read_text()
    except OSError:
        return None
    # Blank out this machine's own HOME first (it may contain spaces), then look for other users' home dirs.
    text = re.sub(re.escape(str(env.home)) + r"(?=[/\"'\s]|$)", "<HOME>", text)
    found = {m.strip() for m in re.findall(r"/(?:Users|home)/[^/\"'\n]+", text)}
    return sorted(found)


# -- generic steps ----------------------------------------------------------------------------------------------
def generic_steps(env):
    who = "Nat, at the target machine's own terminal"
    steps = [
        tool_step(env, "git", "generic", who, "git installed", ["xcode-select --install"], "`git --version` prints a version"),
        tool_step(env, "python3", "generic", who, "python3 installed (orchd is stdlib Python 3.11+)",
                  ["brew install python"], "`python3 --version` prints 3.11 or newer"),
    ]
    p3 = steps[-1]
    if p3.status == PASS:
        rc, out = env.run(["python3", "-c", PY_VERSION_CHECK])
        if out.strip() != "True":
            p3.status, p3.detail = MISSING, p3.detail + "; needs 3.11+ (tomllib)"
    claude = tool_step(env, "claude", "generic", who, "Claude Code installed", ["# install per https://claude.com/claude-code"],
                       "`claude --version` prints a version")
    if claude.status == PASS:
        rc, out = env.run(["claude", "--help"])
        claude.detail += ("; `--bg` listed in help" if "--bg" in out else "; `--bg` NOT in help output")
        if "--bg" not in out:
            claude.status = MISSING
        claude.detail += f". `--messaging-socket-path` is hidden, cannot be verified here ({MIN_NOTE})"
    steps.append(claude)
    steps.append(tool_step(env, "codex", "generic", who, "Codex installed", ["# install per OpenAI Codex docs"],
                           "`codex --version` prints a version"))
    steps.append(tool_step(env, "gh", "generic", who, "GitHub CLI installed", ["brew install gh"], "`gh --version` prints a version"))

    # Logins: the web flow makes the provider show a one-time code / URL to Nat. No token is typed or pasted.
    if env.which("claude"):
        rc, out = env.run(["claude", "auth", "status"])
        try:
            logged_in = json.loads(out).get("loggedIn") is True
        except (ValueError, AttributeError):
            logged_in = False
        st, d = ((PASS, "`claude auth status` exit 0 and reports loggedIn: true") if rc == 0 and logged_in
                 else (UNKNOWN, "`claude auth status` did not report loggedIn: true (failed, unsupported, or logged out); login state unverified"))
    else:
        st, d = MISSING, "claude not installed"
    steps.append(Step("login-claude", "generic", "Claude Code logged in", who, st, d, "`claude auth status` exit code + loggedIn flag (output discarded)",
                      ["claude   # then /login and follow the browser flow"], "re-run this wizard: step shows pass"))
    if env.which("codex"):
        rc, out = env.run(["codex", "login", "status"])
        low = out.lower()
        st, d = ((PASS, "`codex login status` exit 0 and says logged in") if rc == 0 and "logged in" in low and "not logged in" not in low
                 else (UNKNOWN, "`codex login status` did not say logged in (failed or logged out); login state unverified"))
    else:
        st, d = MISSING, "codex not installed"
    steps.append(Step("login-codex", "generic", "Codex logged in", who, st, d, "`codex login status` exit code + wording (output discarded)",
                      ["codex login   # browser flow, do not paste tokens"], "re-run this wizard: step shows pass"))
    gh = gh_state(env)
    if gh is None:
        st, d = MISSING, "gh not installed"
    elif gh_login_ok(gh):
        st, d = PASS, f"gh exit 0, accounts logged in: {', '.join(gh[1])}"
    elif gh[0] == 1 and not gh[1] and not gh[2]:
        st, d = MISSING, "no gh account logged in"
    else:
        st, d = UNKNOWN, (f"`gh auth status` exit {gh[0]}: gh could not confirm every stored login"
                          + (f"; invalid: {', '.join(gh[2])}" if gh[2] else "") + (f"; listed: {', '.join(gh[1])}" if gh[1] else ""))
    steps.append(Step("login-gh", "generic", "GitHub CLI logged in (at least one account)", who, st, d,
                      "`gh auth status` (account names only)",
                      ["gh auth login --web   # run directly in a terminal; not via the `!` prefix, it needs the interactive device code"],
                      "`gh auth status` lists the account"))

    # Git author identity
    ident = []
    for key in ("user.name", "user.email"):
        rc, out = env.run(["git", "config", "--global", "--get", key])
        ident.append(bool(rc == 0 and out.strip()))
    steps.append(Step("git-identity", "generic", "git global author identity set", who, PASS if all(ident) else MISSING,
                      "user.name and user.email set" if all(ident) else "global user.name or user.email missing",
                      "`git config --global user.name/user.email` (set or unset only)",
                      ["git config --global user.name '<your name>'", "git config --global user.email '<your email>'"],
                      "`git config --global user.email` prints your address. Per-repo emails are separate (see nat profile)."))

    # SSH
    ssh_dir = env.home / ".ssh"
    names = sorted(p.name for p in ssh_dir.glob("id_*") if not p.name.endswith(".pub")) if ssh_dir.is_dir() else []
    good = [n for n in names if pub_key_ok(env, ssh_dir / n)]
    st = PASS if good else UNKNOWN if names else MISSING
    steps.append(Step("ssh-keys", "generic", "an SSH key exists for GitHub push", who, st,
                      f"key files: {', '.join(names) or 'none'}; public key valid for: {', '.join(good) or 'none'} "
                      "(private key contents are never read; validity comes from `ssh-keygen -l` on the .pub file)",
                      f"{ssh_dir} directory listing + `ssh-keygen -l -f <key>.pub`",
                      [f"ssh-keygen -t ed25519 -f {q(ssh_dir / 'id_ed25519')}   # generate a NEW key on this machine; never copy private keys from the old one",
                       f"cat {q(ssh_dir / 'id_ed25519.pub')}   # add this PUBLIC key at https://github.com/settings/keys"],
                      "`ssh -T git@github.com` replies `Hi <account>!` (Nat runs it; this wizard does no network call)"))
    steps[-1].detail += "; GitHub accepting the key is UNKNOWN (no network check run)"

    # orchd checkout and Orch home
    steps.append(Step("orchd-checkout", "generic", "orchd checkout present", who,
                      PASS if (env.checkout / "bin" / "orchd").exists() else MISSING,
                      f"{env.checkout}/bin/orchd", "file existence",
                      [f"git clone git@github.com:NatChung/orchd.git {q(env.projects / 'orchd')}"],
                      "`bin/orchd list` prints without error (empty DB is fine)"))
    home = env.orch_home
    exists = (home / "AGENTS.md").exists()
    steps.append(Step("orch-home", "generic", "Orch home present", who, PASS if exists else MISSING,
                      f"{home}" + ("" if exists else " has no AGENTS.md"), "file existence",
                      [f"git clone git@github.com:NatChung/orch.git {q(home)}"], f"{home}/AGENTS.md exists"))
    for label, fn, how in (("Claude", claude_trusted, [f"cd {q(home)} && claude   # accept the trust prompt once, then exit"]),
                           ("Codex", codex_trusted, [f"# Codex Desktop: open {home} as a project and choose Trust (writes ~/.codex/config.toml; Nat does it in the UI)"])):
        st, d = fn(env, home) if exists else (UNKNOWN, "Orch home absent, trust cannot be checked")
        src = "~/.claude.json projects[<path>].hasTrustDialogAccepted" if label == "Claude" else "~/.codex/config.toml projects.<path>.trust_level"
        steps.append(Step(f"trust-orch-{label.lower()}", "generic", f"Orch home trusted in {label}", who, st, d, src, how,
                          "re-run this wizard: step shows pass"))
    cfg = home / ".codex" / "config.toml"
    foreign = foreign_homes(env, cfg)
    if foreign is None:
        st, d = UNKNOWN, f"{cfg} not readable"
    elif foreign:
        st, d = MISSING, f"{cfg} hardcodes another machine's paths: {', '.join(foreign)}"
    else:
        st, d = PASS, f"no foreign home paths in {cfg}"
    steps.append(Step("orch-config-paths", "generic", "Orch .codex/config.toml paths match this HOME", who, st, d,
                      "text scan of the file for /Users/<x> or /home/<x>",
                      [f"# edit {q(cfg)} so every /Users/<old> becomes {q(env.home)} (it is a tracked file in the orch repo: commit on a branch, do not edit blindly)"],
                      "re-run this wizard: step shows pass"))
    mcp = read_toml(env.home / ".codex" / "config.toml")
    srv = bool(((mcp or {}).get("mcp_servers") or {}).get("orchd")) if mcp else None  # presence only; env/args dropped
    present = mcp is not None
    del mcp
    st, d = ((PASS, "[mcp_servers.orchd] present in ~/.codex/config.toml") if srv
             else (UNKNOWN, "~/.codex/config.toml unreadable") if not present
             else (MISSING, "no [mcp_servers.orchd] in ~/.codex/config.toml (needed for the Codex/Astra Orch; the Claude Orch gets its MCP config from `orchd orch`)"))
    steps.append(Step("mcp-orchd-codex", "generic", "orchd MCP registered in Codex", who, st, d, "~/.codex/config.toml mcp_servers.orchd",
                      [f"codex mcp add orchd -- python3 {q(env.checkout / 'bin' / 'orchd')} mcp   # check `codex mcp add --help` for the exact form first"],
                      "new Codex session lists the `orchd` tools"))

    # Repo trust: one row per repo so gaps are named, not summarised.
    repos = sorted(p for p in env.projects.glob("*") if (p / ".git").exists()) if env.projects.is_dir() else []
    for repo in repos:
        st, d = claude_trusted(env, repo)
        steps.append(Step(f"trust-repo-{repo.name}", "generic", f"repo {repo.name} trusted in Claude (workers need it)", who, st, d,
                          "~/.claude.json projects[<path>].hasTrustDialogAccepted",
                          [f"cd {q(repo)} && claude   # accept the trust prompt once, then exit"], "dispatch to this repo no longer says 'Claude has not trusted'"))
    if not repos:
        steps.append(Step("trust-repos", "generic", "product repos cloned and trusted", who, UNKNOWN,
                          f"no git repos under {env.projects}; nothing to check yet", "directory listing", [], ""))

    # State dir and socket dir: checked without creating anything.
    for sid, title, path in (("orchd-home", "orchd state dir writable", env.orchd_home), ("tmp-sockets", "/tmp usable for worker sockets", Path("/tmp"))):
        probe = path
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        ok = os.access(probe, os.W_OK | os.X_OK)
        steps.append(Step(sid, "generic", title, who, PASS if ok else MISSING,
                          f"{probe} {'is' if ok else 'is NOT'} writable (nothing created; orchd makes its own 0700 dirs)",
                          "os.access", [], ""))

    # doctor: optional, never a pass by implication.
    doctor = env.checkout / "orchd" / "doctor.py"
    steps.append(Step("doctor", "generic", "run the read-only `orchd doctor`", who,
                      UNKNOWN,
                      "orchd/doctor.py present in this checkout; the wizard does not run it or read its result" if doctor.exists()
                      else "orchd doctor is not in this checkout (PR #22 unmerged); skip this step, do not treat as pass",
                      "file existence only",
                      [f"{q(env.checkout / 'bin' / 'orchd')} doctor"] if doctor.exists() else [],
                      "doctor exits 0 with no `missing`; any `unknown` or crash is a failure of this step, not a pass"))
    return steps


# -- Nat profile ------------------------------------------------------------------------------------------------
def nat_steps(env):
    who = "Nat, at the target machine's own terminal"
    out = []
    gh = gh_state(env)
    hosts = ssh_hosts(env)
    for acct, (alias, keyfile) in NAT_ACCOUNTS.items():
        key = env.home / ".ssh" / keyfile
        parts, st = [], PASS
        if gh is None or acct not in gh[1] + gh[2]:
            parts.append(f"gh account {acct} not logged in")
            st = MISSING
        elif acct in gh[2]:
            parts.append(f"gh account {acct} has an invalid token")
            st = MISSING
        elif gh[0] != 0:
            parts.append(f"`gh auth status` exit {gh[0]}: login for {acct} not confirmed")
            st = UNKNOWN
        if not key.is_file():
            parts.append(f"{key} missing")
            st = MISSING
        elif not pub_key_ok(env, key):
            parts.append(f"{key}.pub missing or not a valid public key (private file not read)")
            st = UNKNOWN if st == PASS else st
        if alias:
            if hosts is None:
                parts.append("~/.ssh/config unreadable")
                st = UNKNOWN if st == PASS else st
            elif alias not in hosts:
                parts.append(f"Host {alias} not in ~/.ssh/config")
                st = MISSING
        cmds = [f"gh auth login --web   # choose account {acct} in the browser",
                f"ssh-keygen -t ed25519 -f {q(key)} -C {acct}   # new key on this machine; do not copy the old private key"]
        if alias:
            cmds.append(f"# add to {q(env.home / '.ssh' / 'config')}:  Host {alias} / HostName github.com / User git / IdentityFile {q(key)} / IdentitiesOnly yes")
        cmds.append(f"# register {q(str(key) + '.pub')} on GitHub while logged in as {acct} (Nat, in the browser)")
        out.append(Step(f"gh-{acct}", "nat", f"GitHub account {acct}: gh login + SSH key + alias", who, st,
                        "; ".join(parts) or "gh login verified, key pair and alias present (GitHub accepting the key is UNKNOWN: no network check run)",
                        "`gh auth status` account names, ~/.ssh file existence + `ssh-keygen -l` on .pub, ~/.ssh/config Host lines", cmds,
                        f"`ssh -T {alias or 'git@github.com'}` replies `Hi {acct}!`"))
    out.append(Step("git-identity-per-repo", "nat", "per-repo git author email (KC repos use the KC address)", who, UNKNOWN,
                    "repo-local user.email depends on which repo belongs to which identity; not derivable by the wizard", "n/a",
                    ["git -C <repo> config user.email   # compare with docs: ~/.claude/CLAUDE.md identity table"],
                    "each repo prints the expected address before its first commit"))
    for conn in NAT_CONNECTORS:
        d = env.projects / "nat-assistant" / "connectors" / conn
        out.append(Step(f"connector-{conn}", "nat", f"connector {conn}", who, UNKNOWN if d.is_dir() else MISSING,
                        (f"{d} present; token presence NOT checked (token location differs per connector and the wizard never reads secrets)"
                         if d.is_dir() else f"{d} missing (clone nat-assistant first)"),
                        "directory existence", [f"# follow the connector's own SKILL.md to log in; the wizard never copies tokens or exports the old keychain"],
                        "the connector's own read-only status command succeeds"))
    for cli in NAT_CLIS:
        path = env.which(cli)
        out.append(Step(f"cli-{cli}", "nat", f"worker CLI {cli} (report only, not required)", who,
                        PASS if path else MISSING, f"found at {path}" if path else f"`{cli}` not on PATH", "PATH lookup", [], ""))
    return out


def build_plan(env, profile="all"):
    steps = []
    if profile in ("generic", "all"):
        steps += generic_steps(env)
    if profile in ("nat", "all"):
        steps += nat_steps(env)
    return steps


RECEIPTS = """\
Pilot receipts (the pilot is run by Nat on the target machine, or by a worker Nat authorised there; issue #3 stays
open until these exist):
  1. {orchd} doctor                 -> exit 0, no missing/unknown (once PR #22 is merged)
  2. dispatch a throwaway task to a scratch repo, with ORCHD_HOME set to a temp dir
  3. worker: orchd ack, orchd progress, orchd report --status done   -> each shows in `orchd list` / `orchd watch`
  4. an independent reviewer (not the author) reads the diff and writes a review receipt
  5. close the task; the temp ORCHD_HOME and scratch repo are deleted by Nat
Paste the command output of 1-4 into issue #3. A printed plan is not a receipt."""


def render(steps, env):
    lines = [f"orchd setup plan for HOME={env.home}  (read-only: nothing below has been run)", ""]
    order = {"generic": 0, "nat": 1}
    for prof in ("generic", "nat"):
        group = [s for s in steps if s.profile == prof]
        if not group:
            continue
        lines.append(f"== {'Generic environment' if prof == 'generic' else 'Nat profile'} ==")
        for s in group:
            lines.append(f"[{s.status.upper():7}] {s.title}")
            lines.append(f"          {s.detail}   (source: {s.source})")
            if s.status != PASS:
                lines.append(f"          who: {s.who}")
                for c in s.commands:
                    lines.append(f"          $ {c}")
                if s.receipt:
                    lines.append(f"          receipt: {s.receipt}")
        lines.append("")
    counts = {k: sum(s.status == k for s in steps) for k in (PASS, MISSING, UNKNOWN, MANUAL)}
    lines.append("summary: " + ", ".join(f"{v} {k}" for k, v in counts.items()))
    lines.append("")
    lines.append(RECEIPTS.format(orchd=q(env.checkout / "bin" / "orchd")))
    return "\n".join(lines)


def interactive(env, profile, input_fn=input, out=print):
    """Show open steps one at a time; Enter re-checks. Never executes anything. Returns the final plan so --strict
    can judge it, including when the user quits early."""
    for step in build_plan(env, profile):
        if step.status == PASS:
            continue
        while True:
            out(f"\n[{step.status.upper()}] {step.title}\n  {step.detail}\n  who: {step.who}")
            for c in step.commands:
                out(f"  $ {c}")
            if step.receipt:
                out(f"  receipt: {step.receipt}")
            try:
                ans = input_fn("  Do it yourself, then Enter to re-check, 's' to skip, 'q' to quit: ").strip().lower()
            except EOFError:
                ans = "q"
            if ans == "q":
                return build_plan(env, profile)
            if ans == "s":
                break
            fresh = next((s for s in build_plan(env, profile) if s.id == step.id), None)
            if fresh and fresh.status == PASS:
                out("  now pass")
                break
            step = fresh or step
    final = build_plan(env, profile)
    out(render(final, env))
    return final


def main(argv=None, env=None, input_fn=input):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", choices=["generic", "nat", "all"], default="generic")
    ap.add_argument("--home", type=Path)
    ap.add_argument("--projects", type=Path)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--interactive", action="store_true")
    ap.add_argument("--strict", action="store_true")
    args = ap.parse_args(argv)
    home = args.home or Path.home()
    env = env or Env(home=home, projects=args.projects or home / "projects")
    if args.interactive:
        steps = interactive(env, args.profile, input_fn)
        return 1 if args.strict and any(s.status != PASS for s in steps) else 0
    steps = build_plan(env, args.profile)
    print(json.dumps([s.to_dict() for s in steps], indent=2, ensure_ascii=False) if args.json else render(steps, env))
    return 1 if args.strict and any(s.status != PASS for s in steps) else 0


if __name__ == "__main__":
    sys.exit(main())
