"""`orchd init`: create the Orch home and the Desktop interface under ~/orch, trust them, optionally migrate.

Folders and files come from orchd/templates. A file that already exists and differs (Nat edited it, or an older
template) is kept and reported, never overwritten. Codex trust is appended to ~/.codex/config.toml after a timestamped
backup; Claude trust is only checked, never written: several Claude processes rewrite ~/.claude.json and would
clobber an outside edit (docs/decisions.md), so Nat accepts it once in `claude`.
"""
import os
import shutil
import string
import time
from pathlib import Path

from . import paths

TEMPLATES = Path(__file__).resolve().parent / "templates"
# Never copied by --from: version control, OS litter, Claude worktrees, and the files init generates.
MIGRATE_SKIP = {".git", ".gitignore", ".DS_Store", ".claude", ".codex", "AGENTS.md"}

try:
    import tomllib
except ImportError:  # Python < 3.11: trust state cannot be judged
    tomllib = None


def _approvals(server, names):
    return "".join(f'\n[mcp_servers.{server}.tools.{name}]\napproval_mode = "approve"\n' for name in names)


def personal(home, env):
    """~/.config/orchd/init.toml: [home] read = [paths], disabled_apps = [app ids]. Missing file: nothing extra."""
    path = paths.config_dir(home, env) / "init.toml"
    if not path.is_file():
        return [], []
    if tomllib is None:
        raise ValueError(f"{path} needs Python 3.11+ (tomllib) to read")
    section = tomllib.loads(path.read_text()).get("home") or {}
    reads, apps = section.get("read") or [], section.get("disabled_apps") or []
    if not all(isinstance(x, str) and x for x in [*reads, *apps]):
        raise ValueError(f"{path}: [home] read and disabled_apps must be lists of non-empty strings")
    expand = lambda p: str(Path(home) / p[2:]) if p.startswith("~/") else p
    return [expand(p) for p in reads], apps


def _toml_string(text):
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render(relative, values):
    return string.Template((TEMPLATES / relative).read_text()).substitute(values)


def planned_files(home, env, orchd_bin=None):
    """{target path: text} for every file init manages."""
    from . import mcp_server  # imported lazily: it pulls in the runtime
    orch, interface = paths.orch_home(home, env), paths.interface_home(home, env)
    reads, apps = personal(home, env)
    values = dict(HOME=str(Path(home)), ORCH_HOME=str(orch), INTERFACE_HOME=str(interface),
                  ORCHD=str(orchd_bin or paths.orchd_executable(env)),
                  EXTRA_READ="".join(f"{_toml_string(p)} = \"read\"\n" for p in reads),
                  DISABLED_APPS="".join(f"\n[apps.{_toml_string(a)}]\nenabled = false\n" for a in apps))
    files = {}
    for base, target in (("home", orch), ("interface", interface)):
        for source in sorted((TEMPLATES / base).rglob("*")):
            if source.is_file():
                relative = source.relative_to(TEMPLATES)
                text = render(relative, values) if source.name == "config.toml" else source.read_text()
                files[target / source.relative_to(TEMPLATES / base)] = text
    files[orch / ".codex" / "config.toml"] += _approvals("orchd", [t["name"] for t in mcp_server.TOOLS])
    files[interface / ".codex" / "config.toml"] += _approvals("orchd_entry", [t["name"] for t in mcp_server.ENTRY_TOOLS])
    if env.get("ORCHD_HOME"):  # a non-default state dir must reach both MCP servers
        for server, target in (("orchd", orch), ("orchd_entry", interface)):
            files[target / ".codex" / "config.toml"] += f'\n[mcp_servers.{server}.env]\nORCHD_HOME = "{env["ORCHD_HOME"]}"\n'
    return files


def write_managed(path, text):
    if path.exists():
        return "unchanged" if path.read_text() == text else "kept (differs from orchd's version; delete it to regenerate)"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return "created"


def codex_config(home, env):
    return Path(env.get("CODEX_HOME") or Path(home) / ".codex") / "config.toml"


def codex_trust(config, folders):
    """Append a trusted [projects."<folder>"] table per untrusted folder; back up first; re-parse to confirm."""
    if tomllib is None:
        return {str(f): "unknown (Python without tomllib)" for f in folders}
    try:
        text = config.read_text()
    except FileNotFoundError:
        text = ""
    try:
        projects = tomllib.loads(text).get("projects") or {}
    except ValueError as error:
        return {str(f): f"skipped ({config} does not parse: {error})" for f in folders}
    result, missing = {}, []
    for folder in folders:
        key = str(Path(folder).resolve())
        entry = projects.get(key)
        if isinstance(entry, dict) and entry.get("trust_level") == "trusted":
            result[key] = "already trusted"
        elif entry is not None:
            result[key] = f"skipped (has its own [projects] entry: {entry})"
        else:
            missing.append(key)
    if not missing:
        return result
    config.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if config.exists():
        backup = config.with_name(f"{config.name}.orchd-init-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(config, backup)
    addition = "".join(f'\n[projects."{key}"]\ntrust_level = "trusted"\n' for key in missing)
    new = text + ("" if text.endswith("\n") or not text else "\n") + addition
    try:
        tomllib.loads(new)
    except ValueError as error:
        return dict(result, **{k: f"skipped (result would not parse: {error})" for k in missing})
    config.write_text(new)
    for key in missing:
        result[key] = "written" + (f" (backup {backup})" if backup else "")
    return result


def migrate(source, target, stubs=None):
    """Copy what Nat kept in an old Orch home: everything but MIGRATE_SKIP; source untouched.

    A file still holding init's own stub (`stubs`: {path: stub text}) is replaced by the old one; anything else
    that already exists is kept and listed, never overwritten."""
    source, target = Path(source).expanduser().resolve(), Path(target)
    if not source.is_dir():
        raise ValueError(f"{source} is not a folder")
    stubs = stubs or {}
    copied, replaced, kept = [], [], []
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if relative.parts[0] in MIGRATE_SKIP or path.name == ".DS_Store" or not path.is_file():
            continue
        destination = target / relative
        if destination.exists():
            if destination.read_bytes() == path.read_bytes():
                continue
            if stubs.get(destination) is not None and destination.read_text() == stubs[destination]:
                shutil.copy2(path, destination)
                replaced.append(str(relative))
            else:
                kept.append(str(relative))
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
        copied.append(str(relative))
    return dict(source=str(source), copied=copied, replaced_init_stub=replaced, kept_existing=kept)


def init(rt, home=None, env=None, trust=True, source=None, orchd_bin=None):
    home = Path(home or Path.home())
    env = os.environ if env is None else env
    orch, interface = paths.orch_home(home, env), paths.interface_home(home, env)
    planned = planned_files(home, env, orchd_bin)
    files = {str(path): write_managed(path, text) for path, text in planned.items()}
    (orch / "groups").mkdir(parents=True, exist_ok=True)
    result = dict(orch_home=str(orch), interface=str(interface), files=files)
    if source:
        generated = {orch / "AGENTS.md", orch / ".codex" / "config.toml"}  # never replaced by --from
        result["migrated"] = migrate(source, orch, {p: t for p, t in planned.items() if p not in generated})
    if trust:
        result["codex_trust"] = codex_trust(codex_config(home, env), [orch, interface])
        result["claude_trust"] = ("trusted" if rt.claude_trusted(orch.resolve()) else
                                  f"not trusted: run `cd {orch} && claude`, accept the trust prompt, then exit")
    result["next"] = ("Run `orchd interface` to bind the interface to a Claude Orch, then open "
                      f"{interface} in the Codex Desktop app (permissions: Custom (config.toml)).")
    return result
