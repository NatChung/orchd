"""Resolve versioned worker guidance without depending on the product worktree."""
import os
import shlex
import subprocess
import threading
import time
from pathlib import Path

from . import config, paths
from .runtime import redact

ADAPTER = "docs/worker-flow-adapter.md"
ENTRY = "docs/development/delivery.md"
FETCH_TIMEOUT = 12
BRIEF_BUDGET = 30
FETCH_TTL = 300
_fetches = {}
_fetch_lock = threading.Lock()


def powershell_literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def _git(repo, deadline, *args):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise subprocess.TimeoutExpired(args, BRIEF_BUDGET)
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="true")
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace",
                          check=True, timeout=min(FETCH_TIMEOUT, remaining), env=env).stdout.strip()


def _fetch(repo, deadline):
    key = str(repo.resolve())
    if not _fetch_lock.acquire(timeout=max(0, deadline - time.monotonic())):
        raise subprocess.TimeoutExpired("fetch cache lock", BRIEF_BUDGET)
    try:
        cached = _fetches.get(key)
        if cached and time.monotonic() - cached[0] < FETCH_TTL:
            if cached[1] is not None:
                raise cached[1]
            return
        try:
            _git(repo, deadline, "fetch", "--quiet", "origin")
        except (OSError, subprocess.SubprocessError) as error:
            _fetches[key] = (time.monotonic(), error)
            raise
        _fetches[key] = (time.monotonic(), None)
    finally:
        _fetch_lock.release()


def _error(error):
    detail = getattr(error, "stderr", None) or str(error)
    if isinstance(detail, bytes):
        detail = detail.decode(errors="replace")
    return redact(" ".join(detail.split()))[:200]


def _resolve(source, entry, deadline):
    _fetch(source, deadline)
    sha = _git(source, deadline, "rev-parse", "refs/remotes/origin/HEAD^{commit}")
    _git(source, deadline, "cat-file", "-e", f"{sha}:{entry}")
    target = f"{sha}:{entry}"
    if os.name == "nt":
        command = f"git -C {powershell_literal(source)} show {powershell_literal(target)}"
    else:
        command = shlex.join(["git", "-C", str(source), "show", target])
    return sha, command


def brief(repo, projects=None):
    projects = Path(projects) if projects is not None else paths.projects_dir()
    deadline = time.monotonic() + BRIEF_BUDGET
    source = projects / "orchd"
    try:
        sha, command = _resolve(source, ADAPTER, deadline)
        result = f"Worker flow adapter: {ADAPTER} (orchd SHA {sha}). Read with: `{command}`."
    except (OSError, subprocess.SubprocessError) as error:
        result = f"Worker flow adapter: missing ({_error(error)})."
    if repo:
        try:
            hub = config.delivery_flows().get(repo)
            if hub is not None:
                sha, command = _resolve(projects / hub, ENTRY, deadline)
                result += f"\nDelivery flow: {hub}/{ENTRY} (origin SHA {sha}). Read with: `{command}`."
        except (ValueError, OSError, subprocess.SubprocessError) as error:
            result += f"\nDelivery flow: missing for {repo} ({_error(error)})."
    return "\n\n" + result + "\nSHAs are resolved for this brief; a retry may resolve different SHAs."
