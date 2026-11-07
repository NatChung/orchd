"""`orchd upgrade`: reinstall the uv-installed orchd from the same source at the newest commit (#62).

`uv tool upgrade` keeps uv's cached git resolution and misses new commits, so this runs
`uv tool install --force --refresh <source>` with the source read from uv's own receipt. Only the program
changes first, then the newly installed CLI runs versioned home migrations.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from urllib.parse import unquote, urlsplit

try:
    import tomllib
except ImportError:  # pragma: no cover - installs require 3.11
    tomllib = None

PINS = ("rev", "branch", "tag")
PUBLIC_SOURCE = "git+https://github.com/NatChung/orchd.git"


def legacy_source(url):
    """Archive URLs and legacy SSH installs need an explicit public reinstall."""
    if not url:
        return False
    parsed = urlsplit(url)
    path = parsed.path if "://" in url else url.split(":", 1)[-1]
    repo = path.rstrip("/").removesuffix(".git").lower().strip("/")
    return repo == "natchung/orchd-archive" or (
        repo == "natchung/orchd" and (parsed.scheme == "ssh" or "://" not in url))


def installed_commit(prefix):
    """The commit uv recorded for this install (PEP 610 direct_url.json), or None."""
    prefix = Path(prefix)
    for path in (*prefix.glob("lib/python*/site-packages/orchd-*.dist-info/direct_url.json"),
                 *prefix.glob("Lib/site-packages/orchd-*.dist-info/direct_url.json")):
        try:
            return (json.loads(path.read_text()).get("vcs_info") or {}).get("commit_id")
        except (OSError, ValueError):
            return None
    return None


def source(prefix, *, preserve_pin=False):
    """(pip-style source for uv, git URL or None, dropped pin or None) from uv-receipt.toml; ValueError if unusable."""
    receipt = Path(prefix) / "uv-receipt.toml"
    if tomllib is None or not receipt.is_file():
        raise ValueError("not installed with `uv tool install` (no uv-receipt.toml next to this orchd)")
    requirements = (tomllib.loads(receipt.read_text(encoding="utf-8")).get("tool") or {}).get("requirements") or []
    entry = next((r for r in requirements if isinstance(r, dict) and r.get("name") == "orchd"), None)
    if entry is None:
        raise ValueError(f"{receipt} has no orchd requirement")
    pin = next((f"{k}={entry[k]}" for k in PINS if entry.get(k)), None)
    if entry.get("git"):
        url, _, query = entry["git"].partition("?")  # uv records `@branch` as ...?rev=branch
        pin = pin or next((part for part in query.split("&") if part.split("=")[0] in PINS), None)
        spec = "git+" + url
        if preserve_pin and pin:
            spec += "@" + unquote(pin.partition("=")[2])
        return spec, url, pin
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


def selected_remote_commit(git_url, pin, run):
    """Resolve only an unambiguous selected ref; uv resolves anything more complex."""
    if not pin:
        return remote_head(git_url, run)
    ref = unquote(pin.partition("=")[2])
    if re.fullmatch(r"[0-9a-fA-F]{40,64}", ref):
        return ref
    out = run(["git", "ls-remote", git_url, ref, "refs/heads/" + ref,
               "refs/tags/" + ref, "refs/tags/" + ref + "^{}"])
    if out.returncode:
        return None
    allowed = {ref, ref + "^{}"} if ref.startswith("refs/") else {
        "refs/heads/" + ref, "refs/tags/" + ref, "refs/tags/" + ref + "^{}"}
    if ref == "HEAD":
        allowed.add("HEAD")
    rows = [parts for line in out.stdout.splitlines()
            if len(parts := line.split()) == 2 and parts[1] in allowed]
    peeled = {name.removesuffix("^{}"): sha for sha, name in rows if name.endswith("^{}")}
    commits = {peeled.get(name, sha) for sha, name in rows}
    return commits.pop() if len(commits) == 1 else None


def default_run(cmd, *, env=None):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=600, env=env)


INSTALL_METADATA = "orchd-install.json"
CURRENT_POINTER = "orchd-current.json"


def _atomic_json(file, data):
    file.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=file.name + ".", suffix=".tmp", dir=file.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, file)
    finally:
        Path(temporary).unlink(missing_ok=True)


def current_install(prefix=None):
    """The durable anchor and original bin directory for a side-by-side Windows install."""
    prefix = Path(prefix or sys.prefix)
    file = prefix / INSTALL_METADATA
    if not file.is_file():
        return None
    data = json.loads(file.read_text(encoding="utf-8"))
    if (not isinstance(data, dict) or data.get("schema") != 1 or any(not isinstance(data.get(key), str)
                                       or not Path(data[key]).is_absolute() for key in ("anchor", "bin_dir"))):
        raise ValueError(f"invalid Windows install metadata: {file}")
    return data


def stable_executable(prefix=None, name="orchd"):
    """Return the first environment's launcher, whose new invocations follow the current pointer."""
    data = current_install(prefix)
    if data is None:
        return None
    if name not in ("orchd", "orchd-flutter-gate"):
        raise ValueError("unknown orchd entry point")
    executable = Path(data["anchor"]) / "Scripts" / (name + ".exe")
    if not executable.is_file():
        raise ValueError(f"stable Windows launcher is missing: {executable}")
    return executable


def _active_prefix(anchor):
    pointer = anchor / CURRENT_POINTER
    if not pointer.is_file():
        return anchor
    data = json.loads(pointer.read_text(encoding="utf-8"))
    if (not isinstance(data, dict) or data.get("schema") != 1 or not isinstance(data.get("prefix"), str)
            or not Path(data["prefix"]).is_absolute()):
        raise ValueError(f"invalid Windows install pointer: {pointer}")
    target = Path(data["prefix"])
    if target.resolve() != anchor.resolve() and not target.resolve().is_relative_to((anchor.parent / ".orchd-versions").resolve()):
        raise ValueError(f"Windows install pointer is outside its version directory: {target}")
    metadata = current_install(target)
    if metadata is None or Path(metadata["anchor"]).resolve() != anchor.resolve():
        raise ValueError(f"Windows install pointer targets an unrelated environment: {target}")
    return target


def forward_current(argv=None, *, prefix=None, name="orchd", run=subprocess.call):
    """Forward a new CLI process; existing imported modules and interpreters stay untouched."""
    if os.name != "nt" and prefix is None:
        return None
    prefix = Path(prefix or sys.prefix)
    data = current_install(prefix)
    if data is None:
        return None
    if name not in ("orchd", "orchd-flutter-gate"):
        raise ValueError("unknown orchd entry point")
    target = _active_prefix(Path(data["anchor"]))
    if target.resolve() == prefix.resolve():
        return None
    executable = target / "Scripts" / (name + ".exe")
    if not executable.is_file():
        raise ValueError(f"current Windows launcher is missing: {executable}")
    return run([str(executable), *(sys.argv[1:] if argv is None else argv)])


def cli():
    result = forward_current()
    if result is not None:
        return result
    from .cli import main
    return main()


def flutter_cli():
    result = forward_current(name="orchd-flutter-gate")
    if result is not None:
        return result
    from .flutter_gate import cli as main
    return main()


def publish_windows_install(anchor, candidate_prefix, stable_bin_dir, *, expected_current=None):
    """Activate a complete staged install without replacing launchers or any previous environment."""
    anchor, candidate_prefix, stable_bin_dir = (Path(value).resolve()
                                               for value in (anchor, candidate_prefix, stable_bin_dir))
    if candidate_prefix != anchor and not candidate_prefix.is_relative_to((anchor.parent / ".orchd-versions").resolve()):
        raise ValueError("staged Windows install must be inside the anchor's .orchd-versions directory")
    if expected_current is not None and _active_prefix(anchor).resolve() != Path(expected_current).resolve():
        raise ValueError("the active Windows environment changed while this update was staged; retry through the stable launcher")
    for name in ("python.exe", "orchd.exe", "orchd-flutter-gate.exe"):
        if not (candidate_prefix / "Scripts" / name).is_file():
            raise ValueError(f"incomplete staged Windows install: missing Scripts/{name}")
    metadata = {"schema": 1, "anchor": str(anchor), "bin_dir": str(stable_bin_dir)}
    existing = current_install(anchor)
    if existing is not None and existing != metadata:
        raise ValueError("stable Windows installation metadata conflicts with this update")
    _atomic_json(candidate_prefix / INSTALL_METADATA, metadata)
    if candidate_prefix != anchor:
        _atomic_json(anchor / INSTALL_METADATA, metadata)
    if expected_current is not None and _active_prefix(anchor).resolve() != Path(expected_current).resolve():
        raise ValueError("the active Windows environment changed before publication; retry through the stable launcher")
    _atomic_json(anchor / CURRENT_POINTER, {"schema": 1, "prefix": str(candidate_prefix)})
    return candidate_prefix


def _receipt_bin_dir(prefix):
    receipt = tomllib.loads((prefix / "uv-receipt.toml").read_text(encoding="utf-8"))
    for entry in (receipt.get("tool") or {}).get("entrypoints", []):
        if entry.get("name") in ("orchd", "orchd.exe") and entry.get("install-path"):
            return Path(entry["install-path"]).parent
    raise ValueError("uv receipt has no stable orchd entrypoint; reinstall with the Windows installer")


def windows_upgrade(prefix, spec, run=default_run, which=shutil.which, *, bin_dir=None):
    """Install in a fresh tool prefix, validate it, then publish one current-pointer update.

    `spec` is exact, including any selected Git branch, tag or revision. The runner
    accepts env= so tests and local simulations can keep every path disposable.
    """
    from .file_lock import try_lock
    prefix = Path(prefix).resolve()
    metadata = current_install(prefix)
    anchor = Path(metadata["anchor"]) if metadata else prefix
    stable_bin = Path(metadata["bin_dir"]) if metadata else Path(bin_dir or _receipt_bin_dir(prefix))
    uv = which("uv")
    if not uv:
        return 1, ["cannot upgrade: uv is not on PATH."]
    before = installed_commit(prefix)
    active = _active_prefix(anchor)
    if active.resolve() != prefix.resolve():
        return 1, ["this Windows environment was superseded; rerun upgrade through the stable orchd launcher"]
    anchor.mkdir(parents=True, exist_ok=True)
    with (anchor / ".orchd-upgrade.lock").open("a+") as lock:
        try:
            try_lock(lock)
        except BlockingIOError:
            return 1, ["another Windows upgrade is in progress; retry when it finishes"]
        stage = anchor.parent / ".orchd-versions" / uuid.uuid4().hex
        candidate = stage / "tools" / "orchd"
        stage_env = dict(os.environ, UV_TOOL_DIR=str(stage / "tools"), UV_TOOL_BIN_DIR=str(stage / "bin"))
        command = [uv, "tool", "install", "--refresh", "--python", sys.executable, "--from", spec, "orchd"]
        try:
            out = run(command, env=stage_env)
            if out.returncode:
                return out.returncode, ["staged upgrade failed; the current environment remains active:",
                                        (out.stderr or out.stdout).strip()]
            smoke = run([str(candidate / "Scripts" / "orchd.exe"), "--help"])
            if smoke.returncode:
                return smoke.returncode, ["staged CLI validation failed; the current environment remains active:",
                                          (smoke.stderr or smoke.stdout).strip()]
            after = installed_commit(candidate)
            if before and after == before:
                # This candidate has never been published or used by a worker.
                lines = [f"orchd is already the newest selected commit ({before[:7]})."]
                try:
                    if stage.resolve().is_relative_to((anchor.parent / ".orchd-versions").resolve()):
                        shutil.rmtree(stage)
                except OSError:
                    lines.append(f"Unused staged environment retained at {stage}.")
                return 0, [*lines, *post_upgrade(prefix, run)]
            publish_windows_install(anchor, candidate, stable_bin, expected_current=prefix)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            return 1, [f"Windows upgrade was not activated; the current environment remains active: {error}"]
    after = installed_commit(candidate)
    lines = [f"orchd {(before or 'unknown')[:7]} -> {(after or 'unknown')[:7]}",
             f"Windows upgrade activated: {candidate}",
             "Existing processes retain their original environment; new launcher invocations use the updated environment."]
    lines.extend(post_upgrade(candidate, run))
    return 0, lines


def post_upgrade(prefix, run):
    """Use the newly installed code, not modules cached before uv replaced the package."""
    try:
        windows_cli = prefix / "Scripts" / "orchd.exe"
        cli = windows_cli if windows_cli.is_file() else prefix / "bin" / "orchd"
        out = run([str(cli), "upgrade", "--migrations-only"])
        lines = [text.strip() for text in (out.stdout, out.stderr) if text.strip()]
        if out.returncode:
            lines.append("Program upgrade succeeded; home migrations failed. Retry: orchd upgrade --migrations-only")
        return lines
    except Exception as error:
        return [f"Program upgrade succeeded; home migrations failed: {error}",
                "Retry: orchd upgrade --migrations-only"]


def upgrade(prefix=None, run=default_run, which=shutil.which, checkout=None, *, migrations_only=False, dry_run=False,
            windows=None):
    """Returns (exit code, lines to print)."""
    prefix = Path(prefix or sys.prefix)
    windows = os.name == "nt" if windows is None else windows
    if migrations_only or dry_run:
        from . import home_migrations
        return home_migrations.run(dry_run=dry_run)
    checkout = Path(__file__).resolve().parents[1] / "bin" / "orchd" if checkout is None else checkout
    if checkout and Path(checkout).is_file():
        return 1, [f"orchd is running from a checkout ({Path(checkout).parents[1]}): update it with `git pull` there."]
    try:
        spec, git_url, pin = source(prefix, preserve_pin=windows)
    except ValueError as error:
        if windows:
            return 1, [f"cannot upgrade: {error}.",
                       "Bootstrap from a reviewed checkout with scripts/install-windows.ps1, then rerun orchd upgrade."]
        return 1, [f"cannot upgrade: {error}.",
                   "uv install: uv tool install --force --refresh <the URL you installed from>; pipx: pipx upgrade orchd"]
    if legacy_source(git_url):
        if windows:
            return 1, ["This install uses an archive or legacy SSH source.",
                       "Use scripts/install-windows.ps1 from a reviewed public checkout to select its HTTPS source.",
                       "Keep private-history checkouts separate."]
        return 1, ["This install uses an archive or legacy SSH source; switch explicitly to the public HTTPS source.",
                   f"run: uv tool install --force --refresh {PUBLIC_SOURCE}",
                   "Keep private-history checkouts separate; clone https://github.com/NatChung/orchd.git into a new directory.",
                   "Changing a checkout remote does not change uv's recorded installation source."]
    uv = which("uv")
    if not uv:
        if windows:
            return 1, ["cannot upgrade: uv is not on PATH.",
                       "Install uv, reopen PowerShell, then rerun orchd upgrade."]
        return 1, ["cannot upgrade: uv is not on PATH.", f"run by hand: uv tool install --force --refresh {spec}"]
    if windows:
        before = installed_commit(prefix)
        if git_url and before:
            try:
                selected = selected_remote_commit(git_url, pin, run)
            except (OSError, subprocess.SubprocessError):
                selected = None
            if selected and selected.lower() == before.lower():
                return 0, [f"orchd is already at the newest selected commit ({before[:7]}).", *post_upgrade(prefix, run)]
        try:
            code, lines = windows_upgrade(prefix, spec, run, which)
        except (OSError, ValueError) as error:
            return 1, [f"cannot stage Windows upgrade: {error}"]
        if pin:
            lines.insert(0, f"preserving selected Git ref {pin}")
        return code, lines
    before = installed_commit(prefix)
    lines = []
    if pin:
        lines.append(f"was pinned to {pin}; upgrading to the default branch instead")
    if git_url and not pin:
        head = remote_head(git_url, run)
        if head and before and head == before:
            return 0, [f"orchd is already the newest commit ({before[:7]}).", *post_upgrade(prefix, run)]
    out = run([uv, "tool", "install", "--force", "--refresh", spec])
    if out.returncode != 0:
        return out.returncode or 1, lines + ["upgrade failed; the installed orchd is unchanged:",
                                             (out.stderr or out.stdout).strip()]
    after = installed_commit(prefix)
    lines.append(f"orchd {(before or 'unknown')[:7]} -> {(after or 'unknown')[:7]}" if before != after
                 else f"orchd reinstalled at {(after or 'unknown')[:7]}")
    lines.extend(post_upgrade(prefix, run))
    lines.append("Running Orchs and "
                 "Desktop MCP servers keep the old code: start a new Orch with `orchd binding --new` and open a new "
                 "Desktop conversation.")
    return 0, lines
