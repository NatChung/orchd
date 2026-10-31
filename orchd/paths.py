"""Where orchd's folders live. One root (default ~/orch) holds the Orch home, Desktop interface and computer-use session home.

`home` and `env` are injectable so callers that check another user's machine (doctor, setup wizard) derive the
defaults from that user's HOME, not ours. State (the SQLite DB) stays in ORCHD_HOME, outside these folders.
"""
import os
import re
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


def computer_use_home(home=None, env=None):
    return root(home, env) / "gpt-computer-use-worker"


def orch_images(home=None):
    """Claude's already-readable image tree for this Orch cwd (not a new permission)."""
    cwd = Path(home or orch_home()).resolve()
    key = re.sub(r"[^a-zA-Z0-9]", "-", str(cwd))
    if len(key) > 200:
        raise RuntimeError("Orch home exceeds Claude's image-directory key limit (200 characters)")
    return Path("/tmp").resolve() / f"claude-{os.getuid()}" / key


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
    from . import config  # lazy: config imports paths
    value = config.file_projects_dir(env, home)  # validated; unreadable or wrong-shaped file raises ValueError
    if value is not None:
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


_NATIVE_MAGIC = (b"\x7fELF", b"MZ", b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf", b"\xce\xfa\xed\xfe",
                 b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca")


def _head(executable):
    try:
        with open(executable, "rb") as f:
            return f.read(200)
    except OSError:
        return None


def is_python_script(executable):
    """True when `executable` has to be run by a Python interpreter: a `#!...python...` script, or a shebang-less
    `.py` file. A native binary, a script with any other shebang (a shell wrapper) and a file we cannot read or
    classify (missing, unreadable, shebang-less without `.py`) are launched directly: the path is what the
    operator asked for, and guessing "Python" is what broke `/bin/echo` as an override."""
    head = _head(executable)
    if head is None or head.startswith(_NATIVE_MAGIC):
        return False
    if head.startswith(b"#!"):
        return b"python" in head.split(b"\n", 1)[0]
    return str(executable).endswith(".py")


def shebang_interpreter(executable):
    """The interpreter path in a `#!` line, "env" for `#!/usr/bin/env ...`, or None."""
    head = _head(executable)
    if not head or not head.startswith(b"#!"):
        return None
    words = head.split(b"\n", 1)[0][2:].decode("utf-8", "replace").split()
    if not words:
        return None
    return "env" if Path(words[0]).name == "env" else words[0]


def mcp_launch(executable):
    """(command, leading args) to start `<executable> mcp ...`.

    bin/orchd's shebang is the system python3, which may lack psutil, so a Python entry point is run under this
    interpreter. Anything else (a native binary, an ORCHD_EXECUTABLE shell wrapper, an unknown file) is launched
    directly.
    """
    if is_python_script(executable):
        return sys.executable, [executable]
    return executable, []
