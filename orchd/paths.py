"""Where orchd's folders live. One root (default ~/orch) holds the Orch home and the Desktop interface.

`home` and `env` are injectable so callers that check another user's machine (doctor, setup wizard) derive the
defaults from that user's HOME, not ours. State (the SQLite DB) stays in ORCHD_HOME, outside these folders.
"""
import os
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
