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

WORKER_MODEL = "claude-opus-5-5"


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
    def run(self, cmd, cwd=None, timeout=60, check=True):
        return subprocess.run(cmd, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, timeout=timeout, check=check)

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

    def start_worker(self, worktree, sock, brief):
        cmd = [self.claude, "--bg", "--model", WORKER_MODEL, "--dangerously-skip-permissions",
               "--messaging-socket-path", sock, "--settings", '{"crossSessionInbound":"accept"}',
               "--append-system-prompt", brief]
        out = self.run(cmd, cwd=worktree, timeout=60, check=False)
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
