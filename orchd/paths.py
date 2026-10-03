"""Where orchd's folders live. One root (default ~/orch) holds the Orch home and the Desktop interface.

`home` and `env` are injectable so callers that check another user's machine (doctor, setup wizard) derive the
defaults from that user's HOME, not ours. State (the SQLite DB) stays in ORCHD_HOME, outside these folders.
"""
import os
import shutil
import sys
from pathlib import Path


def root(home=None, env=None):
    env = os.environ if env is None else env
    return Path(env.get("ORCHD_ROOT") or Path(home or Path.home()) / "orch")


def orch_home(home=None, env=None):
    env = os.environ if env is None else env
    return Path(env.get("ORCHD_ORCH_HOME") or root(home, env) / "home")


def interface_home(home=None, env=None):
    env = os.environ if env is None else env
    return Path(env.get("ORCHD_INTERFACE_HOME") or root(home, env) / "interface")


def config_dir(home=None, env=None):
    """Personal settings that stay out of the repo: init.toml (extra Orch-home reads, disabled apps) and
    gh-accounts.json (the workers' owner-to-account map)."""
    env = os.environ if env is None else env
    return Path(env.get("ORCHD_CONFIG_DIR") or Path(home or Path.home()) / ".config" / "orchd")


def orchd_executable(env=None):
    """Absolute path of the orchd command that generated configs, Claude Orchs and workers should run.

    ORCHD_EXECUTABLE wins. In a checkout it is bin/orchd. An install (uv tool / pipx) puts the entry point next to
    its own Python, so that one is used before any other `orchd` on PATH.
    """
    env = os.environ if env is None else env
    if env.get("ORCHD_EXECUTABLE"):
        return env["ORCHD_EXECUTABLE"]
    checkout = Path(__file__).resolve().parents[1] / "bin" / "orchd"
    if checkout.is_file():
        return str(checkout)
    sibling = Path(sys.executable).parent / "orchd"
    if sibling.is_file():
        return str(sibling)
    found = shutil.which("orchd")
    if found:
        return found
    raise RuntimeError("cannot find the orchd command; install it (uv tool install ...) or set ORCHD_EXECUTABLE")
