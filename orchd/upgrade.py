"""`orchd upgrade`: reinstall the uv-installed orchd from the same source at the newest commit (#62).

`uv tool upgrade` keeps uv's cached git resolution and misses new commits, so this runs
`uv tool install --force --refresh <source>` with the source read from uv's own receipt. Only the program
changes: ~/orch, the database and ~/.config/orchd are left alone.
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

try:
    import tomllib
except ImportError:  # pragma: no cover - installs require 3.11
    tomllib = None

PINS = ("rev", "branch", "tag")


def installed_commit(prefix):
    """The commit uv recorded for this install (PEP 610 direct_url.json), or None."""
    for path in Path(prefix).glob("lib/python*/site-packages/orchd-*.dist-info/direct_url.json"):
        try:
            return (json.loads(path.read_text()).get("vcs_info") or {}).get("commit_id")
        except (OSError, ValueError):
            return None
    return None


def source(prefix):
    """(pip-style source for uv, git URL or None, dropped pin or None) from uv-receipt.toml; ValueError if unusable."""
    receipt = Path(prefix) / "uv-receipt.toml"
    if tomllib is None or not receipt.is_file():
        raise ValueError("not installed with `uv tool install` (no uv-receipt.toml next to this orchd)")
    requirements = (tomllib.loads(receipt.read_text()).get("tool") or {}).get("requirements") or []
    entry = next((r for r in requirements if isinstance(r, dict) and r.get("name") == "orchd"), None)
    if entry is None:
        raise ValueError(f"{receipt} has no orchd requirement")
    pin = next((f"{k}={entry[k]}" for k in PINS if entry.get(k)), None)
    if entry.get("git"):
        url, _, query = entry["git"].partition("?")  # uv records `@branch` as ...?rev=branch
        pin = pin or next((part for part in query.split("&") if part.split("=")[0] in PINS), None)
        return "git+" + url, url, pin  # upgrade means the default branch's newest commit
    for key in ("directory", "path", "editable"):
        if entry.get(key):
            return entry[key], None, None
    if entry.get("url"):
        return entry["url"], None, None
    raise ValueError(f"{receipt}: unsupported source {entry}")


def remote_head(git_url, run):
    out = run(["git", "ls-remote", git_url, "HEAD"])
    if out.returncode != 0 or not out.stdout.split():
        return None
    return out.stdout.split()[0]


def default_run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=600)


def upgrade(prefix=None, run=default_run, which=shutil.which, checkout=None):
    """Returns (exit code, lines to print)."""
    prefix = Path(prefix or sys.prefix)
    checkout = Path(__file__).resolve().parents[1] / "bin" / "orchd" if checkout is None else checkout
    if checkout and Path(checkout).is_file():
        return 1, [f"orchd is running from a checkout ({Path(checkout).parents[1]}): update it with `git pull` there."]
    try:
        spec, git_url, pin = source(prefix)
    except ValueError as error:
        return 1, [f"cannot upgrade: {error}.",
                   "uv install: uv tool install --force --refresh <the URL you installed from>; pipx: pipx upgrade orchd"]
    uv = which("uv")
    if not uv:
        return 1, ["cannot upgrade: uv is not on PATH.", f"run by hand: uv tool install --force --refresh {spec}"]
    before = installed_commit(prefix)
    lines = []
    if pin:
        lines.append(f"was pinned to {pin}; upgrading to the default branch instead")
    if git_url and not pin:
        head = remote_head(git_url, run)
        if head and before and head == before:
            return 0, [f"orchd is already the newest commit ({before[:7]})."]
    out = run([uv, "tool", "install", "--force", "--refresh", spec])
    if out.returncode != 0:
        return out.returncode or 1, lines + ["upgrade failed; the installed orchd is unchanged:",
                                             (out.stderr or out.stdout).strip()]
    after = installed_commit(prefix)
    lines.append(f"orchd {(before or 'unknown')[:7]} -> {(after or 'unknown')[:7]}" if before != after
                 else f"orchd reinstalled at {(after or 'unknown')[:7]}")
    lines.append("Only the program changed (~/orch, the database and ~/.config/orchd are untouched). Running Orchs and "
                 "Desktop MCP servers keep the old code: start a new Orch with `orchd binding --new` and open a new "
                 "Desktop conversation.")
    return 0, lines
