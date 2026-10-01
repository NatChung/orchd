"""Read-only health check for an orchd machine (issue #3, Part A).

Every check reports pass / fail / warn / unknown and carries a severity:
  required  orchd cannot work without it: a failure exits 1, an unknown (and no failure) exits 2
  optional  integrations other machines may lack (gh, codex, codegraph, rtk ...): never worse than warn
  profile   Nat's own machine layout (4 GitHub accounts, SSH aliases, connectors): only run with --profile nat

Nothing here installs, logs in, trusts, or edits config. The only write is a throwaway 0700 temp dir for the
socket probe, removed in `finally`. Secrets are never read or printed: auth is judged from exit codes and
status flags, token files only by existence.
"""
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

try:  # Python 3.11+. Without it TOML cannot be judged: reported unknown, never guessed with regex
    import tomllib
except ImportError:  # pragma: no cover - depends on interpreter
    tomllib = None

PASS, FAIL, WARN, UNKNOWN = "pass", "fail", "warn", "unknown"
REQUIRED, OPTIONAL, PROFILE = "required", "optional", "profile"

NAT_GH_ACCOUNTS = ("ariontechs", "NatChung", "nat862", "natchung-gif")
NAT_SSH_ALIASES = ("github-nat862", "github-NatChung", "github-natchung-kc")
EXTERNAL_CLIS = ("codegraph", "rtk", "gcloud", "fastlane")

SETUP_HINTS = {
    "claude": "Install Claude Code (https://claude.com/claude-code), then run `claude` once.",
    "claude-login": "Run `claude auth login` yourself in a terminal (needs a browser).",
    "codex-login": "Run `codex login` yourself in a terminal.",
    "orch-trust": "cd into the Orch home, run `claude` once, accept the trust prompt.",
}


@dataclass
class Check:
    name: str
    severity: str
    status: str
    detail: str = ""
    fix: str = ""

    def as_dict(self):
        return {"name": self.name, "severity": self.severity, "status": self.status,
                "detail": self.detail, "fix": self.fix}


def default_runner(cmd, timeout=15):
    """Return (returncode, stdout+stderr). Raises FileNotFoundError / PermissionError / OSError /
    subprocess.TimeoutExpired; checks map those to fail or unknown, never to pass."""
    done = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
    return done.returncode, (done.stdout or "") + (done.stderr or "")


def _bin(name, env_var, home):
    return os.environ.get(env_var) or shutil.which(name) or str(Path(home) / ".local/bin" / name)


class Doctor:
    """`runner`, `home`, `env`, `projects`, `orch_home`, `data_dir`, `tmp_root` are injectable for tests."""

    def __init__(self, runner=default_runner, home=None, env=None, projects=None, orch_home=None,
                 data_dir=None, tmp_root="/tmp", which=shutil.which, profile=None):
        self.runner = runner
        self.home = Path(home) if home else Path.home()
        self.env = os.environ if env is None else env
        self.projects = Path(projects or self.env.get("ORCHD_PROJECTS") or self.home / "projects")
        self.orch_home = Path(orch_home or self.env.get("ORCHD_ORCH_HOME") or self.home / "projects" / "orch")
        self.data_dir = Path(data_dir or self.env.get("ORCHD_HOME") or self.home / ".local/share/orchd")
        self.tmp_root = tmp_root
        self.which = which
        self.profile = profile
        self.claude = self.env.get("ORCHD_CLAUDE") or which("claude") or str(self.home / ".local/bin/claude")
        self.codex = self.env.get("ORCHD_CODEX") or which("codex") or str(self.home / ".local/bin/codex")
        self.checks = []

    # -- helpers ---------------------------------------------------------------
    def add(self, name, severity, status, detail="", fix=""):
        if severity != REQUIRED and status == FAIL:
            status = WARN  # only required checks may fail
        self.checks.append(Check(name, severity, status, detail, fix))

    def _run(self, cmd):
        """(rc, out, error). error is a short string when the probe itself could not run."""
        try:
            rc, out = self.runner(cmd)
            return rc, out, None
        except FileNotFoundError:
            return None, "", "not found"
        except PermissionError:
            return None, "", "permission denied"
        except subprocess.TimeoutExpired:
            return None, "", "timed out"
        except Exception as exc:  # a probe that cannot run is unknown, not a crash and not a pass
            return None, "", f"{type(exc).__name__}"

    @staticmethod
    def _first_line(text):
        return next((l.strip() for l in text.splitlines() if l.strip()), "")

    def _cli(self, label, path, severity, fix):
        rc, out, err = self._run([path, "--version"])
        if err == "not found":
            self.add(f"{label} CLI", severity, FAIL, f"{path} not found", fix)
        elif err == "permission denied":
            self.add(f"{label} CLI", severity, FAIL, f"{path} is not executable by this user", fix)
        elif err:
            self.add(f"{label} CLI", severity, UNKNOWN, f"`{label} --version` {err}")
        elif rc != 0:
            self.add(f"{label} CLI", severity, FAIL, f"`{label} --version` exited {rc}", fix)
        else:
            self.add(f"{label} CLI", severity, PASS, self._first_line(out))
        return rc == 0 and err is None

    # -- checks ----------------------------------------------------------------
    def check_cli_and_flags(self):
        self._cli("git", "git", REQUIRED, "Install git (Xcode command line tools).")
        if self._cli("claude", self.claude, REQUIRED, SETUP_HINTS["claude"]):
            rc, out, err = self._run([self.claude, "--help"])
            if err or rc != 0:
                self.add("claude --bg", REQUIRED, UNKNOWN, f"`claude --help` {err or f'exited {rc}'}")
                self.add("claude --messaging-socket-path", REQUIRED, UNKNOWN, "help unavailable")
            else:
                self.add("claude --bg", REQUIRED, PASS if "--bg" in out else FAIL,
                         "listed in --help" if "--bg" in out else "not in `claude --help`: upgrade Claude Code",
                         "" if "--bg" in out else "Upgrade Claude Code.")
                if "--messaging-socket-path" in out:
                    self.add("claude --messaging-socket-path", REQUIRED, PASS, "listed in --help")
                else:  # hidden flag, and the CLI silently accepts unknown flags: cannot be confirmed offline
                    self.add("claude --messaging-socket-path", REQUIRED, UNKNOWN,
                             "hidden flag; not verifiable without starting a session. "
                             "Confirmed only by the pilot task (`orchd orch`).")
        self._cli("codex", self.codex, OPTIONAL, "Only needed for Codex workers / Astra: install Codex CLI.")

    def check_auth(self):
        rc, out, err = self._run([self.claude, "auth", "status"])
        if err == "not found":
            self.add("claude login", REQUIRED, UNKNOWN, "claude CLI missing")
        elif err:
            self.add("claude login", REQUIRED, UNKNOWN, f"`claude auth status` {err}")
        else:
            try:
                logged_in = json.loads(out).get("loggedIn")
            except (ValueError, AttributeError):
                logged_in = None
            if logged_in is True:
                self.add("claude login", REQUIRED, PASS, "logged in")
            elif logged_in is False or (rc not in (0, None) and logged_in is None and "not logged" in out.lower()):
                self.add("claude login", REQUIRED, FAIL, "not logged in", SETUP_HINTS["claude-login"])
            else:
                self.add("claude login", REQUIRED, UNKNOWN, f"unrecognised `claude auth status` output (exit {rc})")
        rc, out, err = self._run([self.codex, "login", "status"])
        if err == "not found":
            self.add("codex login", OPTIONAL, WARN, "codex CLI missing")
        elif err:
            self.add("codex login", OPTIONAL, UNKNOWN, f"`codex login status` {err}")
        elif rc == 0:
            self.add("codex login", OPTIONAL, PASS, "logged in")
        else:
            self.add("codex login", OPTIONAL, FAIL, f"not logged in (exit {rc})", SETUP_HINTS["codex-login"])

    def _claude_json(self):
        """(data, problem). problem is why ~/.claude.json cannot be judged; None when data is a usable dict."""
        cfg = self.home / ".claude.json"
        try:
            data = json.loads(cfg.read_text())
        except FileNotFoundError:
            return None, f"{cfg} missing"
        except PermissionError:
            return None, f"no permission to read {cfg}"
        except OSError as exc:
            return None, f"cannot read {cfg} ({type(exc).__name__})"
        except ValueError:
            return None, f"{cfg} is not valid JSON"
        if not isinstance(data, dict):
            return None, f"{cfg} is not a JSON object"
        if not isinstance(data.get("projects", {}), dict):
            return None, f"{cfg} has a malformed `projects` entry"
        return data, None

    def _claude_trusted(self, path):
        """True / False when ~/.claude.json answers for this path; None (with self._claude_problem) when it cannot."""
        data, problem = self._claude_json()
        self._claude_problem = problem
        if problem:
            return None
        entry = (data.get("projects") or {}).get(str(path))
        if entry is None:
            return False
        if not isinstance(entry, dict):
            self._claude_problem = f"project entry for {path} is malformed"
            return None
        flag = entry.get("hasTrustDialogAccepted", False)
        if not isinstance(flag, bool):
            self._claude_problem = f"hasTrustDialogAccepted for {path} is not a boolean"
            return None
        return flag

    def check_orch_home(self):
        home = self.orch_home
        if not home.is_dir():
            self.add("orch home", REQUIRED, FAIL, f"{home} is not a directory" if home.exists() else f"{home} missing",
                     f"Create {home} with an AGENTS.md (see CONTEXT.md: Orch 家).")
            return
        try:
            (home / "AGENTS.md").read_text()
            self.add("orch home", REQUIRED, PASS, f"{home} has AGENTS.md")
        except (FileNotFoundError, IsADirectoryError):
            self.add("orch home", REQUIRED, FAIL, f"{home}/AGENTS.md missing or not a file", "Add the Orch's AGENTS.md.")
        except OSError as exc:
            self.add("orch home", REQUIRED, UNKNOWN, f"cannot read {home}/AGENTS.md ({type(exc).__name__})")
        trusted = self._claude_trusted(home)
        if trusted is None:
            self.add("orch home trusted in Claude", REQUIRED, UNKNOWN, f"not verified: {self._claude_problem}")
        else:
            self.add("orch home trusted in Claude", REQUIRED, PASS if trusted else FAIL,
                     "trusted" if trusted else f"{home} not trusted", "" if trusted else SETUP_HINTS["orch-trust"])
        self.check_codex_config(home)

    HOME_PREFIX = re.compile(r"(?<![\w.~/-])/(?:Users|home)/[^/\s\"']+")

    def _foreign_homes(self, text):
        """Home-dir prefixes found in `text` that are not this machine's home. Only the prefix is returned."""
        own = str(self.home)
        text = re.sub(re.escape(own) + r"(?=/|$|[\s\"'])", "<home>", text)  # this machine's own home is fine
        return {m.group(0) for m in self.HOME_PREFIX.finditer(text)}

    def _mcp_foreign_homes(self, servers):
        """{server name: foreign home prefixes} from command / args / cwd only; env and other fields are never read."""
        found = {}
        if not isinstance(servers, dict):
            return found
        for name, spec in servers.items():
            if not isinstance(spec, dict):
                continue
            parts = [spec.get("command"), spec.get("cwd")]
            args = spec.get("args")
            parts += args if isinstance(args, list) else []
            homes = set()
            for part in parts:
                if isinstance(part, str):
                    homes |= self._foreign_homes(part)
            if homes:
                found[str(name)] = homes
        return found

    def check_codex_config(self, home):
        cfg = self.home / ".codex" / "config.toml"
        try:
            text = cfg.read_text()
        except FileNotFoundError:
            self.add("orch home trusted in Codex", OPTIONAL, WARN, f"{cfg} missing (only needed for Astra/Codex)")
            codex = None
        except OSError as exc:
            self.add("orch home trusted in Codex", OPTIONAL, UNKNOWN, f"cannot read {cfg} ({type(exc).__name__})")
            codex = None
        else:
            codex = self._parse_toml(text)
            if isinstance(codex, str):
                self.add("orch home trusted in Codex", OPTIONAL, UNKNOWN, f"not verified: {codex}")
            else:
                projects = codex.get("projects")
                projects = projects if isinstance(projects, dict) else {}
                entry = projects.get(str(home))
                ok = isinstance(entry, dict) and entry.get("trust_level") == "trusted"
                self.add("orch home trusted in Codex", OPTIONAL, PASS if ok else WARN,
                         "trusted" if ok else f"{home} not trusted in {cfg}",
                         "" if ok else "Add it to ~/.codex/config.toml yourself (trust_level = \"trusted\").")
        self.check_config_paths(codex, cfg)

    @staticmethod
    def _parse_toml(text):
        """dict, or a short reason string when the text cannot be parsed."""
        if tomllib is None:
            return "this Python has no tomllib (3.11+); TOML is not guessed with regex"
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            return "config.toml is not valid TOML"
        return data

    def check_config_paths(self, codex, codex_cfg):
        """Trusted project keys and MCP command/args/cwd (Codex + Claude user/project) must not name another home."""
        stale, problems, sources = {}, [], []
        if isinstance(codex, dict):
            sources.append("codex")
            projects = codex.get("projects") if isinstance(codex.get("projects"), dict) else {}
            for p, body in projects.items():
                if isinstance(body, dict) and body.get("trust_level") == "trusted":
                    for h in self._foreign_homes(p):
                        stale.setdefault("trusted project path", set()).add(h)
            for n, h in self._mcp_foreign_homes(codex.get("mcp_servers")).items():
                stale[f"codex MCP {n}"] = h
        elif isinstance(codex, str):
            problems.append(f"codex: {codex}")
        data, problem = self._claude_json()
        if data is not None:
            sources.append("claude")
            for n, h in self._mcp_foreign_homes(data.get("mcpServers")).items():
                stale[f"claude user MCP {n}"] = h
            for path, entry in (data.get("projects") or {}).items():
                if isinstance(entry, dict):
                    for n, h in self._mcp_foreign_homes(entry.get("mcpServers")).items():
                        stale[f"claude project MCP {n}"] = h
        elif not problem.endswith("missing"):
            problems.append(f"claude: {problem}")
        if stale:
            first, homes = sorted(stale.items())[0]
            self.add("config paths match this home", OPTIONAL, WARN,
                     f"{len(stale)} entry(ies) name another home dir, e.g. {first} -> {sorted(homes)[0]}",
                     "Fix those paths by hand if this config was copied from another machine.")
        elif problems or not sources:
            self.add("config paths match this home", OPTIONAL, UNKNOWN,
                     "not verified: " + ("; ".join(problems) if problems else "no readable config source"))
        else:
            self.add("config paths match this home", OPTIONAL, PASS,
                     f"trusted paths and MCP command/args/cwd checked in {', '.join(sources)}: all under {self.home}")

    def check_repos_trust(self):
        try:
            repos = sorted(p for p in self.projects.iterdir() if (p / ".git").exists())
        except FileNotFoundError:
            self.add("projects dir", REQUIRED, FAIL, f"{self.projects} missing", f"mkdir {self.projects}")
            return
        except NotADirectoryError:
            self.add("projects dir", REQUIRED, FAIL, f"{self.projects} is a file, not a directory",
                     f"Remove it and mkdir {self.projects}")
            return
        except OSError as exc:
            self.add("projects dir", REQUIRED, UNKNOWN, f"cannot list {self.projects} ({type(exc).__name__})")
            return
        self.add("projects dir", REQUIRED, PASS, f"{self.projects}: {len(repos)} repos")
        state = [(r, self._claude_trusted(r)) for r in repos]
        if any(t is None for _, t in state):
            self.add("repos trusted in Claude", OPTIONAL, UNKNOWN, f"not verified: {self._claude_problem}")
            return
        untrusted = [r.name for r, t in state if not t]
        self.add("repos trusted in Claude", OPTIONAL, WARN if untrusted else PASS,
                 f"untrusted: {', '.join(untrusted)}" if untrusted else "all trusted",
                 "Run `claude` once in each listed repo and accept the trust prompt." if untrusted else "")

    def check_data_dir(self):
        d = self.data_dir
        if d.exists() and not d.is_dir():
            self.add("orchd data dir", REQUIRED, FAIL, f"{d} exists but is not a directory",
                     f"Move {d} away so orchd can create its data dir.")
            return
        target = d if d.exists() else d.parent
        if not target.exists():
            self.add("orchd data dir", REQUIRED, FAIL, f"{d} cannot be created ({target} missing)")
        elif not target.is_dir():
            self.add("orchd data dir", REQUIRED, FAIL, f"{d} cannot be created ({target} is not a directory)")
        elif os.access(target, os.W_OK | os.X_OK):
            self.add("orchd data dir", REQUIRED, PASS, f"{d} {'exists' if d.exists() else 'will be created'}")
        else:
            self.add("orchd data dir", REQUIRED, FAIL, f"no write permission on {target}")

    def check_socket_dir(self):
        tmp = None
        try:
            tmp = tempfile.mkdtemp(prefix="orchd-doctor-", dir=self.tmp_root)
            os.chmod(tmp, 0o700)
            mode = os.stat(tmp).st_mode & 0o777
            with socket.socket(socket.AF_UNIX) as s:
                s.bind(os.path.join(tmp, "p.sock"))
            if mode == 0o700:
                self.add("socket dir (0700)", REQUIRED, PASS, f"created, bound a unix socket under {self.tmp_root}")
            else:
                self.add("socket dir (0700)", REQUIRED, FAIL, f"mode is {oct(mode)}, not 0700")
        except OSError as exc:
            self.add("socket dir (0700)", REQUIRED, FAIL, f"{type(exc).__name__} under {self.tmp_root}: {exc.strerror}")
        finally:
            if tmp:
                shutil.rmtree(tmp, ignore_errors=True)

    def gh_accounts(self):
        """(usable, broken, error): account names parsed from `gh auth status`; tokens are never shown."""
        rc, out, err = self._run(["gh", "auth", "status"])
        if err:
            return None, None, err
        usable = re.findall(r"Logged in to \S+ account (\S+)", out)
        broken = re.findall(r"Failed to log in to \S+ account (\S+)", out)
        return usable, broken, None

    def check_gh(self):
        usable, broken, err = self.gh_accounts()
        if err == "not found":
            self.add("gh accounts", OPTIONAL, WARN, "gh not installed (needed to open PRs)", "brew install gh")
        elif err:
            self.add("gh accounts", OPTIONAL, UNKNOWN, f"`gh auth status` {err}")
        elif not usable:
            self.add("gh accounts", OPTIONAL, WARN, "no usable gh account", "Run `gh auth login` yourself.")
        else:
            note = f"usable: {', '.join(usable)}" + (f"; broken: {', '.join(broken)}" if broken else "")
            self.add("gh accounts", OPTIONAL, WARN if broken else PASS, note)
        return usable

    def check_externals(self):
        for tool in EXTERNAL_CLIS:
            found = self.which(tool)
            self.add(f"{tool}", OPTIONAL, PASS if found else WARN, found or "not installed (workers may want it)")

    def check_nat_profile(self, usable_gh):
        have = set(usable_gh or [])
        missing = [a for a in NAT_GH_ACCOUNTS if a not in have]
        self.add("nat: 4 gh accounts", PROFILE, WARN if missing else PASS,
                 f"missing/broken: {', '.join(missing)}" if missing else "all 4 usable",
                 "Log in yourself: `gh auth login` per account." if missing else "")
        cfg = self.home / ".ssh" / "config"
        try:
            hosts = set(re.findall(r"^\s*Host\s+(\S+)", cfg.read_text(), re.M))
        except OSError as exc:
            self.add("nat: ssh host aliases", PROFILE, WARN, f"cannot read {cfg} ({type(exc).__name__})")
            hosts = None
        if hosts is not None:
            gone = [h for h in NAT_SSH_ALIASES if h not in hosts]
            self.add("nat: ssh host aliases", PROFILE, WARN if gone else PASS,
                     f"missing: {', '.join(gone)}" if gone else ", ".join(NAT_SSH_ALIASES))
        keys = sorted((self.home / ".ssh").glob("id_ed25519*")) if (self.home / ".ssh").is_dir() else []
        keys = [k for k in keys if k.suffix != ".pub"]
        self.add("nat: ssh keys", PROFILE, PASS if len(keys) >= 4 else WARN, f"{len(keys)} id_ed25519* key files (expect 4)")
        cfgdir = self.home / ".config" / "ariontechs-ops"
        # credential locations per connectors/*/ops.py; existence of a regular file only, content never read
        for label, pattern in (("email tokens", "token-*.json"), ("slack tokens", "slack-token-*.json"),
                               ("line token", "line-token.json")):
            try:
                found = [p for p in sorted(cfgdir.glob(pattern)) if p.is_file()] if cfgdir.is_dir() else []
            except OSError as exc:
                self.add(f"nat: {label}", PROFILE, UNKNOWN, f"cannot list {cfgdir} ({type(exc).__name__})")
                continue
            self.add(f"nat: {label}", PROFILE, PASS if found else WARN,
                     f"{len(found)} {pattern} file(s) present (contents not read)" if found
                     else f"no {pattern} under {cfgdir}")
        for tool in ("slack-tools", "line-tools"):
            p = self.home / "projects" / "nat-assistant" / "connectors" / tool
            self.add(f"nat: {tool} code", PROFILE, PASS if p.is_dir() else WARN,
                     "present" if p.is_dir() else f"{p} missing (code only; credentials checked above)")

    def run_all(self):
        self.check_cli_and_flags()
        self.check_auth()
        self.check_orch_home()
        self.check_repos_trust()
        self.check_data_dir()
        self.check_socket_dir()
        usable = self.check_gh()
        self.check_externals()
        if self.profile == "nat":
            self.check_nat_profile(usable)
        return self.checks


def exit_code(checks):
    if any(c.severity == REQUIRED and c.status == FAIL for c in checks):
        return 1
    if any(c.severity == REQUIRED and c.status == UNKNOWN for c in checks):
        return 2
    return 0


def render(checks):
    mark = {PASS: "ok  ", FAIL: "FAIL", WARN: "warn", UNKNOWN: "????"}
    lines = [f"[{mark[c.status]}] {c.name} ({c.severity}): {c.detail}" for c in checks]
    todo = [f"  - {c.name}: {c.fix}" for c in checks if c.fix and c.status in (FAIL, WARN, UNKNOWN)]
    if todo:
        lines += ["", "What you need to do yourself (doctor never runs these):", *todo]
    unknown = [c.name for c in checks if c.status == UNKNOWN]
    if unknown:
        lines += ["", "Unknown (could not be checked, NOT counted as pass): " + ", ".join(unknown)]
    code = exit_code(checks)
    lines += ["", {0: "required checks: all pass", 1: "required checks: FAILED",
                   2: "required checks: none failed, but some are unknown"}[code]]
    return "\n".join(lines)
