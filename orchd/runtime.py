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

WORKER_MODELS = {"sol": "gpt-6.1-sol", "sonnet": "claude-sonnet-5-5"}
# Claude Orch choices are independent of the worker policy; its current default stays unchanged.
ORCH_MODELS = {"sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5"}
DEFAULT_WORKER_MODEL = "sol"
DEFAULT_ORCH_MODEL = "opus"
CODEX_FLAGS = ["--json", "--dangerously-bypass-approvals-and-sandbox"]
USAGE_FIELDS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


def redact(text):
    """Drop credentials embedded in URLs (scheme://user:token@host) from error text we keep. The userinfo runs
    to the last @ before the host's path, so passwords holding a raw or encoded @ go too; every URL is done."""
    return re.sub(r"(://)[^\s/?#'\"]*@", r"\1***@", text)


def _text(value):
    return (value.decode(errors="replace") if isinstance(value, bytes) else value or "").strip()


def error_detail(e):
    """Text for a failed command, redacted: its stderr, else its stdout, else the exception text. A timeout says
    so and keeps whatever the command wrote before it was killed."""
    if isinstance(e, subprocess.TimeoutExpired):
        out = [f"{Path(str(e.cmd[0] if isinstance(e.cmd, (list, tuple)) else e.cmd)).name} timed out after "
               f"{e.timeout:g}s", _text(e.stderr), _text(e.output)]
        return redact(": ".join(part for part in out if part))
    if isinstance(e, subprocess.CalledProcessError):
        return redact(_text(e.stderr) or _text(e.output) or str(e))
    return redact(str(e)) or type(e).__name__


def worker_kind(model):
    """A worker runs on the CLI of its model's vendor: GPT models on codex, the rest on claude."""
    return "codex" if model and model.startswith("gpt-") else "claude"


CLAUDE_DEAD_STATES = ("failed",)  # the only dead state observed so far (a reaped worker); "blocked" is alive, waiting


def claude_job_alive(jobs, job_id):
    """Liveness of a Claude job from one `live_jobs()` snapshot: None when the query failed or there is no job,
    False when the daemon no longer lists it or lists it as dead."""
    if jobs is None or not job_id:
        return None
    job = jobs.get(job_id)
    return job is not None and (job or {}).get("state") not in CLAUDE_DEAD_STATES


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

    def spawn(self, cmd, cwd, log):
        """Start a detached process that outlives us, stdout+stderr appended to `log`; return its pid."""
        with open(log, "a") as out:
            return subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                    env=launch_env(), start_new_session=True).pid

    def pid_alive(self, pid):
        try:  # a finished child of this long-lived process stays a zombie, and kill(0) finds it, until reaped
            if os.waitpid(int(pid), os.WNOHANG)[0]:
                return False
        except (ChildProcessError, ValueError):
            pass
        try:
            os.kill(int(pid), 0)
        except (OSError, ValueError):
            return False
        return True

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

    def dirty(self, path):
        """Porcelain status that no git config can thin out: showUntrackedFiles=no and submodule `ignore`
        settings would otherwise hide exactly the files a removal deletes."""
        return self.run(["git", "-c", "status.showUntrackedFiles=all", "-C", path, "status", "--porcelain",
                         "--untracked-files=all", "--ignore-submodules=none"]).stdout.strip()

    def submodule_paths(self, worktree):
        """Absolute paths of every initialized submodule, nested ones included."""
        out = self.run(["git", "-C", worktree, "submodule", "foreach", "--recursive", "--quiet",
                        'echo "$toplevel/$sm_path"']).stdout
        return [line for line in out.splitlines() if line.strip()]

    def submodule_loss(self, worktree):
        """Why this worktree's initialized submodules cannot be removed, else None.
        Removing the worktree deletes the submodules' git dirs too (stashes, local branches, tags, reflog), and
        git only removes a worktree holding submodules with --force, which checks nothing. Anything that cannot
        be proven safe to delete is therefore kept: a clean-at-check submodule is no reason to force."""
        held = None
        for path in self.submodule_paths(worktree):
            name = os.path.relpath(os.path.realpath(path), os.path.realpath(worktree))
            if self.dirty(path):
                return f"uncommitted changes in submodule {name}"
            if self.run(["git", "-C", path, "stash", "list"]).stdout.strip():
                return f"stash in submodule {name}"
            unpushed = self.run(["git", "-C", path, "rev-list", "--count", "HEAD", "--branches", "--tags",
                                 "--not", "--remotes"]).stdout.strip()
            if unpushed != "0":
                return f"commits not pushed in submodule {name}"
            held = held or (f"initialized submodule {name}: git can only remove it with --force, which would "
                            "delete its git dir unchecked; remove the worktree by hand after saving what you need")
        return held

    def worktree_state(self, worktree, base):
        """Return (removable, reason). Removable only when nothing local would be lost."""
        if not self.exists(worktree):
            return True, "worktree already gone"
        if self.dirty(worktree):
            return False, "uncommitted changes"
        loss = self.submodule_loss(worktree)
        if loss:
            return False, loss
        head = self.run(["git", "-C", worktree, "rev-parse", "HEAD"]).stdout.strip()
        if head == base:
            return True, "no new commits"
        upstream = self.run(["git", "-C", worktree, "rev-parse", "@{u}"], check=False)
        if upstream.returncode != 0:  # pushed without -u still counts if origin has this exact commit
            branch = self.run(["git", "-C", worktree, "rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
            remote = self.run(["git", "-C", worktree, "ls-remote", "origin", f"refs/heads/{branch}"],
                              timeout=60, check=False).stdout.split()
            if remote and remote[0] == head:
                return True, "pushed"
            return False, "commits not pushed (no upstream)"
        if upstream.stdout.strip() != head:
            return False, "local branch differs from its upstream"
        return True, "pushed"

    def remove_worktree(self, repo_path, worktree, base=None):
        """Remove a worktree the caller found safe; never with --force. Returns None when removed, or the reason
        it was kept because something unsaved showed up after the caller's check. Raises RuntimeError carrying
        git's stderr when git itself fails. The state is re-checked right before removal (`base` given) and git's
        own clean check runs with untracked files forced visible, so only a sub-millisecond window remains, and
        git still refuses a dirty tree inside it. The branch ref survives removal, so late commits are not lost."""
        if not self.exists(worktree):
            return None
        try:
            if base is not None:
                removable, reason = self.worktree_state(worktree, base)
                if not removable:
                    return reason
            self.run(["git", "-c", "status.showUntrackedFiles=all", "-C", str(repo_path), "worktree", "remove",
                      worktree])
        except subprocess.CalledProcessError as e:
            detail = error_detail(e)
            if "contains modified or untracked files" in detail:
                return "uncommitted changes appeared; worktree kept"
            raise RuntimeError(f"git worktree remove failed (exit {e.returncode}): {detail}") from None
        return None

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

    def stop_task_worker(self, kind, job, marks=(), wait=10.0):
        """Stop one task's worker and confirm it is gone; raise RuntimeError when that cannot be confirmed (the
        stop failed or timed out while the worker still runs, or its state cannot be read). Only this job/pid is
        touched. Callers keep the worktree and the task open on error, so a later call simply retries."""
        if kind == "codex":
            return self._stop_codex_confirmed(job, [m for m in marks if m], wait)
        try:
            result = self.run([self.claude, "stop", job], timeout=30, check=False)
            stop_note = (f"exit {result.returncode}: " + redact(_text(result.stderr) or _text(result.stdout))
                         if result.returncode else "stop sent")
        except (OSError, subprocess.SubprocessError) as e:
            stop_note = error_detail(e)
        state = "job list unavailable"
        for _ in range(max(1, int(wait / 0.5))):
            jobs = self.live_jobs()
            listed = jobs.get(job) if jobs is not None else None
            if jobs is not None and (listed is None or (listed.get("pid") is None and listed.get("status") is None)
                                     or (listed.get("pid") and not self.pid_alive(listed["pid"]))):
                # stopped jobs drop out of the list, and pid/status are listed only while the process is alive
                # (https://code.claude.com/docs/en/agent-view#list-sessions-as-json). `state` is the task outcome:
                # state=failed can still carry a live pid, so it proves nothing here.
                return None
            state = ("job list unavailable" if jobs is None else
                     f"still listed as {listed.get('state')}, pid {listed.get('pid')}, status {listed.get('status')}")
            self.sleep(0.5)
        raise RuntimeError(f"claude worker {job} not confirmed stopped ({state}); claude stop: {stop_note}")

    def _stop_codex_confirmed(self, pid, marks, wait):
        """A Codex turn's pid may be long gone and reused, so only a process that is codex *and* runs this task's
        worktree or thread is ours; anything else means our worker already exited."""
        def ours():
            if not self.pid_alive(pid):
                return False
            try:
                ps = self.run(["ps", "-ww", "-p", str(pid), "-o", "args="], timeout=10, check=False)
            except (OSError, subprocess.SubprocessError) as e:
                raise RuntimeError(f"codex worker {pid} not confirmed stopped: {error_detail(e)}") from None
            if ps.returncode not in (0, 1):
                raise RuntimeError(f"codex worker {pid} not confirmed stopped: ps exit {ps.returncode}: "
                                   f"{redact(_text(ps.stderr))}")
            args = ps.stdout
            return "codex" in args and any(m in args for m in marks)
        if not ours():
            return None
        try:
            os.killpg(int(pid), 15)
        except ProcessLookupError:
            return None
        except (OSError, ValueError) as e:
            raise RuntimeError(f"codex worker {pid} not confirmed stopped: kill failed: {e}") from None
        for _ in range(max(1, int(wait / 0.2))):
            self.sleep(0.2)
            if not ours():
                return None
        raise RuntimeError(f"codex worker {pid} still running {wait:g}s after SIGTERM; not confirmed stopped")

    def live_jobs(self):
        try:
            return {a.get("id"): a for a in self.agents()}
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            return None

    # -- codex workers ----------------------------------------------------------
    # `codex exec` runs one turn and exits, so a Codex worker is a chain of processes on one thread:
    # `exec` for the task, then `exec resume` for each answer. job_id holds the current turn's pid.
    def codex_log(self, task_id):
        return str(Path(self.socket_path(task_id)).parent / "codex.jsonl")

    def start_codex_worker(self, worktree, log, prompt, model):
        pid = self.spawn([self.codex, "exec", *CODEX_FLAGS, "-m", model, "-C", worktree, prompt], worktree, log)
        for _ in range(150):
            for line in Path(log).read_text(errors="replace").splitlines() if self.exists(log) else []:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("type") == "thread.started" and event.get("thread_id"):
                    return str(pid), event["thread_id"]
            if not self.pid_alive(pid):
                break
            self.sleep(0.2)
        self.stop_codex(pid)
        raise RuntimeError("codex exec started no thread: " + Path(log).read_text(errors="replace")[-500:]
                           if self.exists(log) else "codex exec wrote no log")

    def resume_codex_worker(self, worktree, log, thread, text, model):
        """Resume falls back to config.toml's model unless told, so pass the task's model every turn."""
        return str(self.spawn([self.codex, "exec", "resume", *CODEX_FLAGS, "-m", model, thread, text], worktree, log))

    def stop_codex(self, pid):
        """An idle worker's pid is long gone and may be reused, so kill only a process that is still codex."""
        comm = self.run(["ps", "-p", str(pid), "-o", "comm="], check=False).stdout
        if "codex" not in comm:
            return
        try:
            os.killpg(int(pid), 15)
        except (OSError, ValueError):
            pass

    def codex_usage(self, thread):
        """Token totals from the thread's rollout, mapped onto Claude's fields (codex input includes cached)."""
        root = Path(os.environ.get("ORCHD_CODEX_SESSIONS", Path.home() / ".codex" / "sessions"))
        files = sorted(root.glob(f"**/rollout-*-{thread}.jsonl"))
        if not files:
            return None
        total, turns = None, 0
        for line in files[0].read_text(errors="replace").splitlines():
            try:
                payload = json.loads(line).get("payload") or {}
            except (ValueError, AttributeError):
                continue
            if payload.get("type") == "token_count" and (payload.get("info") or {}).get("total_token_usage"):
                total, turns = payload["info"]["total_token_usage"], turns + 1
        if total is None:
            return None
        cached = total.get("cached_input_tokens") or 0
        return {"input_tokens": (total.get("input_tokens") or 0) - cached, "output_tokens": total.get("output_tokens") or 0,
                "cache_creation_input_tokens": total.get("cache_write_input_tokens") or 0,
                "cache_read_input_tokens": cached, "messages": turns}

    def open_codex_viewer(self, worktree, thread):
        self.run(["open", "-na", "Ghostty.app", "--args", f"--working-directory={worktree}", "-e",
                  self.codex, "resume", thread], timeout=30)

    # -- orch and viewer --------------------------------------------------------
    def wake_orch(self, codex_bin, thread, text):
        self.run([codex_bin, "queue", "--thread", thread, "--message", text], timeout=60)

    def open_viewer(self, job):
        self.run(["open", "-na", "Ghostty.app", "--args", "-e", self.claude, "attach", job], timeout=30)
