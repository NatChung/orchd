#!/usr/bin/env python3
"""Check a built orchd wheel in isolation (issue #7). Used by CI; safe to run locally.

  check-package.py DIST_DIR

1. The wheel contains the hidden home/interface `.codex` templates and the worker gh wrapper.
2. `uv tool install` of that wheel into throwaway HOME, UV_TOOL_DIR, UV_TOOL_BIN_DIR, cache and ORCHD_* dirs.
3. `orchd --help` resolves through PATH from the isolated bin dir, outside any checkout.
4. `orchd doctor --json --no-input` before init: valid JSON, Orch home reported as "not set up yet",
   nothing written under the isolated Orch root. Without a signed-in provider CLI it exits 1 (or 2).

It never starts a daemon or an Orch, never signs in, and never touches the real HOME or uv tool dir.
Exit code 0 when every check passes.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

REQUIRED_MEMBERS = (
    "orchd/templates/home/.codex/config.toml",
    "orchd/templates/interface/.codex/config.toml",
    "orchd/worker_bin/gh",
)


def fail(message):
    print(f"FAIL: {message}", file=sys.stderr)
    sys.exit(1)


def check_wheel(dist):
    wheels = sorted(Path(dist).glob("orchd-*.whl"))
    if len(wheels) != 1:
        fail(f"expected exactly one orchd wheel in {dist}, found {[w.name for w in wheels]}")
    wheel = wheels[0]
    with zipfile.ZipFile(wheel) as zf:
        names = set(zf.namelist())
        missing = [m for m in REQUIRED_MEMBERS if m not in names]
        if missing:
            fail(f"{wheel.name} is missing {missing}")
        mode = (zf.getinfo("orchd/worker_bin/gh").external_attr >> 16) & 0o777
    print(f"ok   wheel {wheel.name} contains {', '.join(REQUIRED_MEMBERS)} (gh mode {oct(mode)})")
    return wheel


def isolated_env(root):
    home = root / "home"
    home.mkdir()
    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV")
           and not k.startswith(("ORCHD_", "CODEX_", "CLAUDE_", "UV_TOOL_", "XDG_"))}
    env.update(HOME=str(home), USERPROFILE=str(home), PYTHONUTF8="1",
               UV_TOOL_DIR=str(root / "tools"), UV_TOOL_BIN_DIR=str(root / "bin"),
               UV_CACHE_DIR=str(root / "uv-cache"), UV_NO_CONFIG="1",
               XDG_CONFIG_HOME=str(home / ".config"), XDG_DATA_HOME=str(home / ".local" / "share"),
               ORCHD_HOME=str(root / "state"), ORCHD_ROOT=str(home / "orch"),
               ORCHD_CONFIG_DIR=str(home / "settings"), ORCHD_PROJECTS=str(home / "projects"),
               CODEX_HOME=str(home / ".codex"))
    env["PATH"] = str(root / "bin") + os.pathsep + env.get("PATH", "")
    return env, home


def run(cmd, env, cwd, timeout=60):
    return subprocess.run(cmd, env=env, cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def main(argv):
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    wheel = check_wheel(argv[0]).resolve()
    uv = shutil.which("uv")
    if not uv:
        fail("uv is required")
    with tempfile.TemporaryDirectory(prefix="orchd-wheel-") as tmp:
        root = Path(tmp)
        env, home = isolated_env(root)
        install = run([uv, "tool", "install", "--python", sys.executable, str(wheel)], env, root, timeout=300)
        if install.returncode:
            fail(f"uv tool install exited {install.returncode}\n{install.stdout}{install.stderr}")
        on_path = shutil.which("orchd", path=env["PATH"])
        if not on_path or Path(on_path).parent.resolve() != (root / "bin").resolve():
            fail(f"`orchd` on the isolated PATH resolves to {on_path}, not {root / 'bin'}")
        print("ok   isolated uv tool install; `orchd` resolves from the isolated bin dir on PATH")

        installed = [p for m in REQUIRED_MEMBERS for p in (root / "tools").rglob(m.split("/", 1)[1])
                     if p.as_posix().endswith(m)]
        if len(installed) < len(REQUIRED_MEMBERS):
            fail(f"installed tool is missing packaged files: found {installed}")
        print("ok   installed tool contains the .codex templates and worker gh wrapper")

        helped = run(["orchd", "--help"] if os.name != "nt" else [on_path, "--help"], env, root)
        if helped.returncode or "dispatch" not in helped.stdout:
            fail(f"`orchd --help` exited {helped.returncode}\n{helped.stdout}{helped.stderr}")
        print("ok   `orchd --help` exits 0")

        doctor = run([on_path, "doctor", "--json", "--no-input"], env, root)
        try:
            checks = json.loads(doctor.stdout)
        except json.JSONDecodeError:
            fail(f"`orchd doctor --json` printed non-JSON (exit {doctor.returncode})\n{doctor.stdout}{doctor.stderr}")
        by_name = {c["name"]: c for c in checks}
        orch_home = by_name.get("orch home", {})
        if orch_home.get("status") != "warn" or "not set up yet" not in orch_home.get("detail", ""):
            fail(f"doctor before init should warn that the Orch home is not set up: {orch_home}")
        if (home / "orch").exists():
            fail("doctor wrote under the isolated Orch root")
        if doctor.returncode not in (0, 1, 2):
            fail(f"doctor exited {doctor.returncode}\n{doctor.stderr}")
        failed = sorted(c["name"] for c in checks if c["severity"] == "required" and c["status"] == "fail")
        print(f"ok   doctor before init exits {doctor.returncode}; Orch home not set up; "
              f"required failures: {', '.join(failed) or 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
