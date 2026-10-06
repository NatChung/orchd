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


def projects_dir(home=None, env=None):
    """Shared project root for doctor and dispatch, independent of the caller's cwd.

    Explicit env/config wins; a checkout under the target HOME uses its parent.
    Installed copies keep ~/projects as the fallback.
    """
    env = os.environ if env is None else env
    home = Path(home or Path.home())

    def expand(value):
        # Expand against the target HOME, including checks for another user.
        if value == "~":
            return home
        if value.startswith("~/"):
            return home / value[2:]
        path = Path(value)
        if not path.is_absolute():
            raise ValueError("projects_dir must be an absolute path or start with ~/")
        return path

    if env.get("ORCHD_PROJECTS"):
        return expand(env["ORCHD_PROJECTS"])
    config = config_dir(home, env) / "config.toml"
    try:
        text = config.read_text(encoding="utf-8") if config.exists() else None
    except FileNotFoundError:
        pass
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"cannot read {config} ({type(exc).__name__})") from exc
    else:
        if text is None:
            data = {}
        else:
            try:
                import tomllib
            except ImportError as exc:
                raise ValueError("reading orchd config.toml requires Python 3.11+") from exc
            try:
                data = tomllib.loads(text)
            except tomllib.TOMLDecodeError as exc:
                raise ValueError(f"invalid TOML in {config}") from exc
        value = data.get("projects_dir")
        if value is not None:
            if not isinstance(value, str) or not value:
                raise ValueError(f"projects_dir in {config} must be a non-empty string")
            return expand(value)
    checkout = Path(__file__).resolve().parents[1]
    if checkout.is_relative_to(home.resolve()) and (checkout / ".git").is_dir():
        return home / checkout.relative_to(home.resolve()).parent
    return home / "projects"


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
