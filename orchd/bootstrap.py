"""`orchd init`: create Orch, interface and computer-use session homes under ~/orch.

Folders and files come from orchd/templates. A file that already exists and differs (Operator edited it, or an older
template) is kept and reported, except computer-use AGENTS.md uses managed-block updates.
Codex trust is appended to ~/.codex/config.toml after a timestamped backup; Claude trust is only checked, never written: several Claude processes rewrite ~/.claude.json and would
clobber an outside edit (docs/decisions.md), so Operator accepts it once in `claude`.
"""
import json
import os
import re
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
    section = tomllib.loads(path.read_text(encoding="utf-8")).get("home") or {}
    reads, apps = section.get("read") or [], section.get("disabled_apps") or []
    if not all(isinstance(x, str) and x for x in [*reads, *apps]):
        raise ValueError(f"{path}: [home] read and disabled_apps must be lists of non-empty strings")
    expand = lambda p: str(Path(home) / p[2:]) if p.startswith("~/") else p
    return [expand(p) for p in reads], apps


def _toml_string(text):
    return json.dumps(str(text), ensure_ascii=False)


def render(relative, values):
    return string.Template((TEMPLATES / relative).read_text(encoding="utf-8")).substitute(values)


def interface_instructions(interface):
    source = Path(interface) / "AGENTS.md"
    if not source.exists():
        source = TEMPLATES / "interface" / "AGENTS.md"
    return (source.read_text(encoding="utf-8")
            + "\nIf orchd_entry tools are deferred, use functions.exec to find their names "
              "in ALL_TOOLS and call them through the tools object. Check ALL_TOOLS "
              "before reporting that status or relay is unavailable.\n")


def sync_interface_instructions(interface, backup_dir, *, dry_run=False):
    """Refresh the marked Windows instruction snapshot without changing any policy or model."""
    if os.name != "nt":
        return False
    file = Path(interface) / ".codex" / "config.toml"
    if not file.is_file():
        return False
    text = file.read_bytes().decode("utf-8")
    marker = "# orchd:interface-instructions"
    if marker not in text:
        return False  # existing, user-managed config must be explicitly regenerated
    rules = interface_instructions(interface)
    old = tomllib.loads(text)
    if old.get("developer_instructions") == rules:
        return False
    pattern = re.compile(r"(?m)^" + re.escape(marker) + r"(\r?\n)developer_instructions = [^\r\n]*(\r?\n|$)")
    new, count = pattern.subn(lambda m: marker + m[1] + "developer_instructions = " + _toml_string(rules) + m[2], text)
    if count != 1 or tomllib.loads(new) != dict(old, developer_instructions=rules):
        raise ValueError(f"{file}: cannot safely update managed interface instructions; regenerate this config")
    if not dry_run:
        from . import home_migrations
        home_migrations.write_file(file, new.encode("utf-8"), Path(backup_dir))
    return True


def sync_interface_entry_tools(interface, backup_dir, *, dry_run=False):
    """Pre-approve entry tools added after init in an existing interface config; returns report lines.

    Only appends missing [mcp_servers.orchd_entry.tools.NAME] tables; every other setting stays as it is. On Windows
    an orchd-generated developer_instructions line from before the managed marker existed is also refreshed (and
    marked, so later rule updates reach it); a hand-written one is left alone.
    """
    from . import mcp_server
    file = Path(interface) / ".codex" / "config.toml"
    if tomllib is None or not file.is_file():
        return []
    text = file.read_bytes().decode("utf-8")
    old = tomllib.loads(text)
    server = (old.get("mcp_servers") or {}).get("orchd_entry")
    if not isinstance(server, dict):
        return [f"skipped: {file} has no [mcp_servers.orchd_entry] (user-managed config)"]
    names = [t["name"] for t in mcp_server.ENTRY_TOOLS]
    missing = [name for name in names if name not in (server.get("tools") or {})]
    new, lines = text, []
    marker = "# orchd:interface-instructions"
    legacy = old.get("developer_instructions")
    if (os.name == "nt" and marker not in text and isinstance(legacy, str)
            and legacy.startswith("<!-- orchd:managed:start")):
        rules = interface_instructions(interface)
        pattern = re.compile(r"(?m)^developer_instructions = [^\r\n]*(\r?\n|$)")
        new, count = pattern.subn(lambda m: marker + (m[1] or "\n") + "developer_instructions = "
                                  + _toml_string(rules) + m[1], new)
        if count != 1 or tomllib.loads(new).get("developer_instructions") != rules:
            raise ValueError(f"{file}: cannot safely update the interface instructions; regenerate this config")
        lines.append("interface instructions refreshed")
    if missing:
        new += ("" if new.endswith("\n") else "\n") + _approvals("orchd_entry", missing)
        lines.append("pre-approved entry tools: " + ", ".join(missing))
    if new == text:
        return [f"unchanged: {file}"]
    parsed = tomllib.loads(new)
    if ({k: v for k, v in parsed.items() if k not in ("developer_instructions", "mcp_servers")}
            != {k: v for k, v in old.items() if k not in ("developer_instructions", "mcp_servers")}
            or set(parsed["mcp_servers"]["orchd_entry"]["tools"]) < set(names)):
        raise ValueError(f"{file}: entry tool update would change other settings; regenerate this config")
    if dry_run:
        return [f"would update {file}: " + "; ".join(lines)]
    from . import home_migrations
    backup = home_migrations.write_file(file, new.encode("utf-8"), Path(backup_dir))
    return [f"updated: {file}: " + "; ".join(lines) + f"; backup: {backup}",
            "Restart the Codex app (or open a new interface conversation) to load the new entry tools."]


def planned_files(home, env, orchd_bin=None):
    """{target path: text} for every file init manages."""
    from . import mcp_server  # imported lazily: it pulls in the runtime
    orch, interface = paths.orch_home(home, env), paths.interface_home(home, env)
    reads, apps = personal(home, env)
    from . import config
    interface_model, interface_effort = config.interface_settings(env, home)
    command, lead = paths.mcp_launch(str(orchd_bin or paths.orchd_executable(env)))
    interface_bootstrap = ""
    if os.name == "nt":
        # Restricted Windows profiles cannot bootstrap sandboxed AGENTS.md reads
        # on every Codex backend. Keep the policy intact and supply the Entry's
        # instructions through the trusted project config instead.
        rules = interface_instructions(interface)
        interface_bootstrap = ("project_doc_max_bytes = 0\n"
                               "# orchd:interface-instructions\n"
                               f"developer_instructions = {_toml_string(rules)}\n")
    values = dict(HOME=str(Path(home)), ORCH_HOME=str(orch), INTERFACE_HOME=str(interface),
                  HOME_PATH=_toml_string(home), ORCH_HOME_PATH=_toml_string(orch),
                  INTERFACE_HOME_PATH=_toml_string(interface),
                  LOCAL_BIN_PATH=_toml_string(Path(home) / ".local" / "bin"),
                  CODEX_STANDALONE_PATH=_toml_string(Path(home) / ".codex" / "packages" / "standalone"),
                  ORCH_CONFIG_PATH=_toml_string(orch / ".codex"),
                  PLATFORM_READS='' if os.name == "nt" else '"/opt/homebrew" = "read"\n',
                  WINDOWS_SANDBOX='\n[windows]\nsandbox = "elevated"\n' if os.name == "nt" else '',
                  INTERFACE_BOOTSTRAP=interface_bootstrap,
                  ORCHD_COMMAND=_toml_string(command),
                  ORCHD_LEAD="".join(_toml_string(a) + ", " for a in lead),
                  INTERFACE_MODEL=_toml_string(interface_model), INTERFACE_EFFORT=_toml_string(interface_effort),
                  ENTRY_APPROVALS=_approvals("orchd_entry", [t["name"] for t in mcp_server.ENTRY_TOOLS]),
                  EXTRA_READ="".join(f"{_toml_string(p)} = \"read\"\n" for p in reads),
                  DISABLED_APPS="".join(f"\n[apps.{_toml_string(a)}]\nenabled = false\n" for a in apps))
    files = {}
    for base, target in (("home", orch), ("interface", interface),
                         ("gpt-computer-use-worker", paths.computer_use_home(home, env))):
        for source in sorted((TEMPLATES / base).rglob("*")):
            if source.is_file():
                relative = source.relative_to(TEMPLATES)
                text = render(relative, values) if source.name == "config.toml" else source.read_text(encoding="utf-8")
                files[target / source.relative_to(TEMPLATES / base)] = text
    files[orch / ".codex" / "config.toml"] += _approvals("orchd", [t["name"] for t in mcp_server.TOOLS])
    # Desktop MCP servers do not inherit the shell that ran init.
    overrides = {key: env[key] for key in ("ORCHD_HOME", "ORCHD_CONFIG_DIR") if env.get(key)}
    if env.get("ORCHD_PROJECTS"):
        overrides["ORCHD_PROJECTS"] = str(paths.projects_dir(home, env))
    if overrides:
        for server, target in (("orchd", orch), ("orchd_entry", interface)):
            files[target / ".codex" / "config.toml"] += (
                f'\n[mcp_servers.{server}.env]\n'
                + "".join(f"{key} = {_toml_string(value)}\n" for key, value in overrides.items()))
    return files


def write_managed(path, text):
    if path.exists():
        return "unchanged" if path.read_text(encoding="utf-8") == text else "kept (differs from orchd's version; delete it to regenerate)"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return "created"


def codex_config(home, env):
    return Path(env.get("CODEX_HOME") or Path(home) / ".codex") / "config.toml"


def codex_trust(config, folders):
    """Append a trusted [projects."<folder>"] table per untrusted folder; back up first; re-parse to confirm."""
    if tomllib is None:
        return {str(f): "unknown (Python without tomllib)" for f in folders}
    try:
        text = config.read_text(encoding="utf-8")
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
    addition = "".join(f'\n[projects.{_toml_string(key)}]\ntrust_level = "trusted"\n' for key in missing)
    new = text + ("" if text.endswith("\n") or not text else "\n") + addition
    try:
        tomllib.loads(new)
    except ValueError as error:
        return dict(result, **{k: f"skipped (result would not parse: {error})" for k in missing})
    config.write_text(new, encoding="utf-8")
    for key in missing:
        result[key] = "written" + (f" (backup {backup})" if backup else "")
    return result


def migrate(source, target, stubs=None):
    """Copy what Operator kept in an old Orch home: everything but MIGRATE_SKIP; source untouched.

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
            if stubs.get(destination) is not None and destination.read_text(encoding="utf-8") == stubs[destination]:
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
    from . import channel_layout
    layout_env = dict(env)
    if not layout_env.get("ORCHD_ROOT") and not layout_env.get("ORCHD_CHANNEL_HOME"):
        layout_env["ORCHD_ROOT"] = str(paths.root(home, env))
    channel_layout.plan(home=home, env=layout_env)
    orch, interface = paths.orch_home(home, env), paths.interface_home(home, env)
    planned = planned_files(home, env, orchd_bin)
    from . import home_migrations
    worker = paths.computer_use_home(home, env)
    worker_rules = worker / "AGENTS.md"
    files = {str(path): write_managed(path, text) for path, text in planned.items() if path != worker_rules}
    existed = worker_rules.exists()
    lines = []
    home_migrations.update_rules({"gpt-computer-use-worker": worker}, False, lines,
                                 home_migrations.state_dir(home, env) / "backups")
    files[str(worker_rules)] = ("unchanged" if lines[0].startswith("unchanged:") else
                                "updated" if existed else "created")
    if sync_interface_instructions(interface, home_migrations.state_dir(home, env) / "backups" / "interface-config"):
        files[str(interface / ".codex" / "config.toml")] = "updated (managed interface instructions)"
    (worker / "reports").mkdir(parents=True, exist_ok=True)
    (orch / "groups").mkdir(parents=True, exist_ok=True)
    result = dict(orch_home=str(orch), interface=str(interface), computer_use_worker=str(worker),
                  computer_use_rules=lines, files=files)
    if source:
        generated = {orch / "AGENTS.md", orch / ".codex" / "config.toml"}  # never replaced by --from
        result["migrated"] = migrate(source, orch, {p: t for p, t in planned.items() if p not in generated})
    if trust:
        result["codex_trust"] = codex_trust(codex_config(home, env), [orch, interface])
        result["claude_trust"] = ("trusted" if rt.claude_trusted(orch.resolve()) else
                                  f"not trusted: run `cd {orch} && claude`, accept the trust prompt, then exit")
    from . import home_migrations
    home_migrations.record_init(home, env)
    from . import channel_layout
    result["channel_layout"] = channel_layout.apply(home=home, env=layout_env)
    result["layout_version"] = result["channel_layout"]["layout_version"]
    result["next"] = ("Run `orchd binding` to bind the interface to a Claude Orch, then open "
                      f"{interface} in the Codex Desktop app (permissions: interface).")
    return result


def install(rt, home=None, env=None, trust=True, dry_run=False, orchd_bin=None):
    """Provision the normal homes and the disabled channel layout."""
    home = Path(home or Path.home())
    env = os.environ if env is None else env
    if dry_run:
        from . import channel_layout
        layout_env = dict(env)
        if not layout_env.get("ORCHD_ROOT") and not layout_env.get("ORCHD_CHANNEL_HOME"):
            layout_env["ORCHD_ROOT"] = str(paths.root(home, env))
        return {"layout_version": channel_layout.VERSION,
                "channel_layout": channel_layout.plan(home=home, env=layout_env),
                "files": [str(p) for p in planned_files(home, env, orchd_bin)],
                "dry_run": True}
    return init(rt, home=home, env=env, trust=trust, orchd_bin=orchd_bin)
