"""Everything that touches git, the claude/codex CLIs, sockets, or the terminal.

Commands go through `Runtime.run` so tests can replace it. Claude background
sessions and the UDS frame follow agentctl's verified path (see
reference/agentctl/docs/native.md); the messaging socket flag is hidden and
version-dependent, so failures raise instead of falling back.
"""
import json
import os
import re
import shutil
import socket
import subprocess
import time
import uuid
from pathlib import Path

ORCHD_BIN = str(Path(__file__).resolve().parents[1] / "bin" / "orchd")

MODELS = {"sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5"}
DEFAULT_WORKER_MODEL = "sonnet"
DEFAULT_ORCH_MODEL = "opus"
USAGE_FIELDS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


def launch_env():
    """Keep a parent Claude session's variables and Orch id out of the sessions we start (belt and braces:
    the Claude daemon may spawn --bg sessions from its own environment anyway)."""
    return {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE") and k != "ORCHD_ORCH_ID"}


def mcp_env(orch_id):
    """--bg sessions are spawned by the Claude daemon, not by us, so only env written into the MCP config
    reaches the Orch's MCP server."""
    env = {"ORCHD_ORCH_ID": orch_id}
    if os.environ.get("ORCHD_HOME"):
        env["ORCHD_HOME"] = os.environ["ORCHD_HOME"]
    return env


def _bin(name, env_var):
    found = os.environ.get(env_var) or shutil.which(name)
    if found:
        return found
    fallback = Path.home() / ".local/bin" / name
    return str(fallback)


class Runtime:
    def __init__(self):
        self.claude = _bin("claude", "ORCHD_CLAUDE")
        self.codex = _bin("codex", "ORCHD_CODEX")
        self.projects = Path(os.environ.get("ORCHD_PROJECTS", Path.home() / "projects"))

    # -- process plumbing (tests override these) --------------------------------
    def run(self, cmd, cwd=None, timeout=60, check=True, env=None):
        return subprocess.run(cmd, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, timeout=timeout, check=check, env=env)

    def send_uds(self, path, session_id, text):
        frame = {"type": "user", "session_id": session_id, "uuid": str(uuid.uuid4()),
                 "from": "orchd", "priority": "next", "message": {"role": "user", "content": text}}
        with socket.socket(socket.AF_UNIX) as peer:
            peer.settimeout(5)
            peer.connect(path)
            peer.sendall((json.dumps(frame, ensure_ascii=False) + "\n").encode())
            peer.shutdown(socket.SHUT_WR)

    def sleep(self, seconds):
        time.sleep(seconds)

    def exists(self, path):
        return os.path.exists(path)

    # -- repos and worktrees ----------------------------------------------------
    def repo_path(self, repo):
        path = (self.projects / repo).resolve()
        if path.parent != self.projects.resolve() or not (path / ".git").exists():
            raise ValueError(f"{repo} is not a git repo directly under {self.projects}")
        return path

    def claude_trusted(self, repo_path):
        """Claude keys trust by the repo's main checkout; worktrees inherit it, subdirs of ~/projects do not."""
        try:
            data = json.loads((Path.home() / ".claude.json").read_text())
        except (OSError, ValueError):
            return False
        return bool((data.get("projects") or {}).get(str(repo_path), {}).get("hasTrustDialogAccepted"))

    def base_ref(self, repo_path):
        self.run(["git", "-C", str(repo_path), "fetch", "--quiet", "origin"], timeout=60, check=False)
        head = self.run(["git", "-C", str(repo_path), "symbolic-ref", "--quiet", "--short",
                         "refs/remotes/origin/HEAD"], check=False)
        return head.stdout.strip() if head.returncode == 0 and head.stdout.strip() else "HEAD"

    def create_worktree(self, repo_path, repo, task_id):
        base = self.base_ref(repo_path)
        base_commit = self.run(["git", "-C", str(repo_path), "rev-parse", base]).stdout.strip()
        branch = f"orchd/{task_id}"
        path = self.projects / ".orchd-worktrees" / f"{repo}-{task_id}"
        path.parent.mkdir(parents=True, exist_ok=True)
        self.run(["git", "-C", str(repo_path), "worktree", "add", "-b", branch, str(path), base_commit])
        return base_commit, branch, str(path)

    def worktree_state(self, worktree, base):
        """Return (removable, reason). Removable only when nothing local would be lost."""
        if not self.exists(worktree):
            return True, "worktree already gone"
        dirty = self.run(["git", "-C", worktree, "status", "--porcelain"]).stdout.strip()
        if dirty:
            return False, "uncommitted changes"
        head = self.run(["git", "-C", worktree, "rev-parse", "HEAD"]).stdout.strip()
        if head == base:
            return True, "no new commits"
        upstream = self.run(["git", "-C", worktree, "rev-parse", "@{u}"], check=False)
        if upstream.returncode != 0:
            return False, "commits not pushed (no upstream)"
        if upstream.stdout.strip() != head:
            return False, "local branch differs from its upstream"
        return True, "pushed"

    def remove_worktree(self, repo_path, worktree):
        if self.exists(worktree):
            self.run(["git", "-C", str(repo_path), "worktree", "remove", worktree])

    # -- claude workers ---------------------------------------------------------
    def socket_path(self, task_id):
        # macOS limits UDS paths to ~104 bytes, so keep them short and private.
        directory = Path("/tmp") / f"orchd-{task_id}"
        directory.mkdir(mode=0o700, exist_ok=True)
        return str(directory / "w.sock")

    def agents(self):
        out = self.run([self.claude, "agents", "--json"], timeout=15).stdout
        data = json.loads(out)
        if not isinstance(data, list):
            raise RuntimeError("unsupported claude agents JSON format")
        return data

    def start_claude(self, cwd, sock, model, extra_args):
        cmd = [self.claude, "--bg", "--model", model, *extra_args, "--messaging-socket-path", sock]
        out = self.run(cmd, cwd=cwd, timeout=60, check=False, env=launch_env())
        match = re.search(r"claude attach ([a-zA-Z0-9-]+)", out.stdout)
        if not match:
            raise RuntimeError("claude --bg returned no job id: " + (out.stdout + out.stderr)[-500:])
        job = match[1]
        for _ in range(125):
            agent = next((a for a in self.agents() if a.get("id") == job), None)
            if agent and agent.get("sessionId") and self.exists(sock):
                return job, agent["sessionId"]
            self.sleep(0.2)
        raise RuntimeError(f"claude job {job} did not publish its session and socket")

    def start_worker(self, worktree, sock, brief, model):
        return self.start_claude(worktree, sock, model, [
            "--dangerously-skip-permissions", "--settings", '{"crossSessionInbound":"accept"}',
            "--append-system-prompt", brief])

    def orch_socket_path(self, orch_id):
        directory = Path("/tmp") / f"orchd-o-{orch_id}"
        directory.mkdir(mode=0o700, exist_ok=True)
        directory.chmod(0o700)  # the session crashes before init on any other mode
        return str(directory / "o.sock")

    def start_orch(self, orch_id, model, orch_home):
        orch_home = Path(orch_home)
        sock = self.orch_socket_path(orch_id)
        mcp = Path(sock).parent / "mcp.json"
        mcp.write_text(json.dumps({"mcpServers": {"orchd": {
            "command": "/usr/bin/python3", "args": [ORCHD_BIN, "mcp"], "env": mcp_env(orch_id)}}}))
        try:
            agents = (orch_home / "AGENTS.md").read_text()
        except OSError:
            agents = ""
        prompt = f"You are an orchd Orch. Your orch id is {orch_id}. Instructions from AGENTS.md follow:\n{agents}"
        settings = {"crossSessionInbound": "accept",
                    "permissions": {"allow": ["Read", "Edit", "Write", "mcp__orchd"]}}
        job, session = self.start_claude(orch_home, sock, model, [
            "--restricted", "--permission-mode", "dontAsk", "--strict-mcp-config", "--mcp-config", str(mcp),
            "--settings", json.dumps(settings), "--append-system-prompt", prompt])
        return sock, job, session

    def attach(self, job):
        os.execvp(self.claude, [self.claude, "attach", job])

    def claude_usage(self, session_id):
        """Token totals of a session's transcript; a message id can span several lines, so count each once."""
        root = Path(os.environ.get("ORCHD_CLAUDE_PROJECTS", Path.home() / ".claude" / "projects"))
        files = sorted(root.glob(f"*/{session_id}.jsonl"))
        if not files:
            return None
        seen = {}
        for line in files[0].read_text(errors="replace").splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            msg = entry.get("message") if entry.get("type") == "assistant" else None
            if isinstance(msg, dict) and isinstance(msg.get("usage"), dict):
                seen[msg.get("id") or entry.get("uuid") or len(seen)] = msg["usage"]
        total = {k: sum(u.get(k) or 0 for u in seen.values()) for k in USAGE_FIELDS}
        return {**total, "messages": len(seen)}

    def stop_worker(self, job):
        self.run([self.claude, "stop", job], timeout=30, check=False)

    def live_jobs(self):
        try:
            return {a.get("id"): a for a in self.agents()}
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            return None

    # -- orch and viewer --------------------------------------------------------
    def wake_orch(self, codex_bin, thread, text):
        self.run([codex_bin, "queue", "--thread", thread, "--message", text], timeout=60)

    def open_viewer(self, job):
        self.run(["open", "-na", "Ghostty.app", "--args", "-e", self.claude, "attach", job], timeout=30)
