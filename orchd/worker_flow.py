"""Resolve versioned worker guidance without depending on the product worktree."""
import shlex
import subprocess
from pathlib import Path

from . import config, paths

ADAPTER = "docs/worker-flow-adapter.md"
ENTRY = "docs/development/delivery.md"


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          check=True, timeout=60).stdout.strip()


def brief(repo, projects=None):
    projects = Path(projects) if projects is not None else paths.projects_dir()
    source = projects / "orchd"
    try:
        sha = _git(source, "rev-parse", "HEAD^{commit}")
        _git(source, "cat-file", "-e", f"{sha}:{ADAPTER}")
        command = shlex.join(["git", "-C", str(source), "show", f"{sha}:{ADAPTER}"])
        result = f"Worker flow adapter: {ADAPTER} (orchd SHA {sha}). Read with: `{command}`."
    except (OSError, subprocess.SubprocessError):
        result = f"Worker flow adapter: missing ({source}/{ADAPTER} at orchd HEAD)."
    if not repo:
        return "\n\n" + result
    try:
        mapping = config.delivery_flows()
        hub = mapping.get(repo)
        if hub is None:
            return "\n\n" + result
        source = projects / hub
        _git(source, "fetch", "--quiet", "origin")
        sha = _git(source, "rev-parse", "refs/remotes/origin/HEAD^{commit}")
        _git(source, "cat-file", "-e", f"{sha}:{ENTRY}")
        command = shlex.join(["git", "-C", str(source), "show", f"{sha}:{ENTRY}"])
        result += f"\nDelivery flow: {hub}/{ENTRY} (origin SHA {sha}). Read with: `{command}`."
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        result += f"\nDelivery flow: missing for {repo} (configuration, Hub, fetch, origin HEAD, or entry unavailable: {type(error).__name__})."
    return "\n\n" + result
