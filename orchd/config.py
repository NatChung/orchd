"""Personal orchd settings: ~/.config/orchd/config.toml (ORCHD_CONFIG_DIR), next to init.toml.

    projects_dir = "/Users/me/projects"   # top-level; env ORCHD_PROJECTS wins, see paths.projects_dir
    [worker]
    model = "sol"        # sol | sonnet; with nothing set anywhere, sonnet

Worker model precedence: dispatch's explicit model (flag) > ORCHD_WORKER_MODEL (env) > config file > default.
Every layer is validated even when a higher one wins; a bad value raises ValueError naming its source.
Only None (flag) and an empty ORCHD_WORKER_MODEL count as "not given". A wrong-shaped file fails closed.
Every write (worker.model, projects_dir; the doctor's first-run prompt uses it too) goes through one in-place edit of
the one line (line endings, CRLF included, are kept byte for byte, a new file is 0600 in a 0700 directory, an existing file keeps its
mode) and refuses (writing nothing) unless the re-parsed result equals the old data apart from that key, so nothing else is rewritten.
ORCHD_CONFIG_FILE points at an exact file; it is a test/advanced hook and outranks ORCHD_CONFIG_DIR.
Review always uses the other vendor than the author, so it has no setting.
"""
import json
import os
import re
import tempfile
from pathlib import Path

from . import paths
from .runtime import WORKER_MODELS

try:
    import tomllib
except ImportError:  # Python < 3.11
    tomllib = None

FACTORY_WORKER_MODEL = "sonnet"
KEYS = {"worker-model": ("worker", "model"), "projects-dir": (None, "projects_dir")}  # (table or None for top level, key)
_ANY_TABLE = re.compile(r"^\s*\[")


def path(env=None, home=None):
    env = os.environ if env is None else env
    if env.get("ORCHD_CONFIG_FILE"):
        return Path(env["ORCHD_CONFIG_FILE"])
    return paths.config_dir(home, env) / "config.toml"


def path_source(env=None):
    env = os.environ if env is None else env
    if env.get("ORCHD_CONFIG_FILE"):
        return "ORCHD_CONFIG_FILE"
    return "ORCHD_CONFIG_DIR" if env.get("ORCHD_CONFIG_DIR") else "default"


def _read(env=None, home=None):
    """(text, data); ("", {}) when the file is absent."""
    file = path(env, home)
    if not file.exists():
        return "", {}
    if not file.is_file():
        raise ValueError(f"{file}: not a file")
    if tomllib is None:
        raise ValueError(f"{file} needs Python 3.11+ (tomllib) to read")
    try:
        with open(file, newline="", encoding="utf-8") as handle:  # keep \r\n as is (Path.read_text(newline=) is 3.13+)
            text = handle.read()
    except (OSError, UnicodeError) as error:
        raise ValueError(f"{file}: cannot read ({type(error).__name__})") from None
    try:
        return text, tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise ValueError(f"{file}: invalid TOML: {error}") from None


def _check_model(value, source):
    if not isinstance(value, str):
        raise ValueError(f"{source}: worker model must be a string, one of {', '.join(WORKER_MODELS)}")
    if value not in WORKER_MODELS:
        raise ValueError(f"{source}: unknown worker model {value!r}; use one of {', '.join(WORKER_MODELS)}")
    return value


def _check_projects_dir(value, source):
    if not isinstance(value, str) or not value:
        raise ValueError(f"projects_dir in {source} must be a non-empty string")
    if value != "~" and not value.startswith("~/") and not Path(value).is_absolute():
        raise ValueError("projects_dir must be an absolute path or start with ~/")
    return value


_CHECKS = {"worker-model": _check_model, "projects-dir": _check_projects_dir}


def _check(value, source, key="worker-model"):
    return _CHECKS[key](value, source)


def _file_value(data, file, key, validate=True):
    """The file's value for a KEYS entry (validated unless told otherwise) or None; a wrong-shaped file raises."""
    table, name = KEYS[key]
    section = data
    if table is not None:
        if table not in data:
            return None
        section = data[table]
        if not isinstance(section, dict):
            raise ValueError(f"{file}: [{table}] must be a table, got {type(section).__name__}")
    if name not in section:
        return None
    return _check(section[name], str(file), key) if validate else section[name]


def worker_model(explicit=None, env=None):
    """(model alias, source) with source one of flag, env, file, default."""
    env = os.environ if env is None else env
    flag = None if explicit is None else _check_model(explicit, "flag (dispatch model)")
    raw = env.get("ORCHD_WORKER_MODEL")
    from_env = _check_model(raw, "ORCHD_WORKER_MODEL") if raw else None
    from_file = _file_value(_read(env)[1], path(env), "worker-model")
    for value, source in ((flag, "flag"), (from_env, "env"), (from_file, "file")):
        if value is not None:
            return value, source
    return FACTORY_WORKER_MODEL, "default"


def file_projects_dir(env=None, home=None):
    """projects_dir as written in the config file (validated), or None; used by paths.projects_dir."""
    return _file_value(_read(env, home)[1], path(env, home), "projects-dir")


def intake_producers(env=None, home=None):
    """{producer: {providers, accounts}} from [intake.producers.NAME] (read only; edit the file by hand).
    Absent means no producer is allowed. A wrong-shaped section raises ValueError, so intake fails closed."""
    file = path(env, home)
    section = _read(env, home)[1].get("intake", {})
    if not isinstance(section, dict) or not isinstance(section.get("producers", {}), dict):
        raise ValueError(f"{file}: [intake.producers] must be tables")
    out = {}
    for name, grant in section.get("producers", {}).items():
        lists = grant.get("providers"), grant.get("accounts")
        if not isinstance(grant, dict) or not all(isinstance(v, list) and v and all(isinstance(i, str) for i in v)
                                                  for v in lists):
            raise ValueError(f"{file}: [intake.producers.{name}] needs non-empty string lists providers and accounts")
        out[name] = dict(providers=lists[0], accounts=lists[1])
    return out


def _edit(text, table, name, value):
    """Set (str) or remove (None) one key by editing lines in place; table None is the top level."""
    eol = "\r\n" if "\r\n" in text else "\n"  # inserted lines follow the file's own style
    lines = text.split("\n")
    lines = [line + "\n" for line in lines[:-1]] + ([lines[-1]] if lines[-1] else [])
    new = None if value is None else f"{name} = {json.dumps(value, ensure_ascii=False)}{eol}"
    key = re.compile(rf"^\s*{re.escape(name)}\s*=")
    if table is None:
        start, end = -1, next((i for i, line in enumerate(lines) if _ANY_TABLE.match(line)), len(lines))
    else:
        header = re.compile(rf"^\s*\[\s*{re.escape(table)}\s*\]\s*(#.*)?$")
        start = next((i for i, line in enumerate(lines) if header.match(line)), None)
        if start is None:
            if new is None:
                return text
            sep = "" if not text else (eol if text.endswith("\n") else eol * 2)
            return text + sep + f"[{table}]" + eol + new
        end = next((i for i in range(start + 1, len(lines)) if _ANY_TABLE.match(lines[i])), len(lines))
    at = next((i for i in range(start + 1, end) if key.match(lines[i])), None)
    if at is None:
        if new is not None:
            lines.insert(start + 1, new)  # top level: first line of the file, above any tables
    elif new is None:
        del lines[at]
    else:
        lines[at] = new.rstrip("\r\n") + ("\r\n" if lines[at].endswith("\r\n") else "\n")  # keep that line's own ending
    return "".join(lines)


def _save(old_text, old_data, key, value, env=None, home=None):
    file = path(env, home)
    table, name = KEYS[key]
    new_text = _edit(old_text, table, name, value)
    expected = {k: (dict(v) if k == table and isinstance(v, dict) else v) for k, v in old_data.items()}
    section = expected if table is None else (expected.setdefault(table, {}) if value is not None else expected.get(table))
    if value is not None:
        section[name] = value
    elif isinstance(section, dict):
        section.pop(name, None)
    try:
        ok = tomllib.loads(new_text) == expected
    except tomllib.TOMLDecodeError:
        ok = False
    if not ok:
        raise ValueError(f"{file}: cannot edit {name} safely here (dotted or inline form?); "
                         "edit the file by hand. Nothing was written")
    if new_text == old_text:
        return
    file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=file.parent, prefix=file.name + ".", suffix=".tmp")  # 0600, unique per writer
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as handle:
            handle.write(new_text)
        if file.exists():
            os.chmod(tmp, file.stat().st_mode & 0o777)
        os.replace(tmp, file)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _key(key):
    if key not in KEYS:
        raise ValueError(f"unknown config key {key!r}; use one of {', '.join(KEYS)}")
    return key


def set_value(key, value, env=None, home=None):
    _key(key)
    _check(value, "value", key)
    text, data = _read(env, home)
    _file_value(data, path(env, home), key, validate=False)  # a bad current value must stay fixable
    _save(text, data, key, value, env, home)


def unset_value(key, env=None, home=None):
    _key(key)
    text, data = _read(env, home)
    _file_value(data, path(env, home), key, validate=False)  # a bad current value must stay fixable
    _save(text, data, key, None, env, home)


def get_value(key, env=None):
    """(effective value as a string, source) for any KEYS entry."""
    env = os.environ if env is None else env
    if _key(key) == "worker-model":
        return worker_model(env=env)
    if env.get("ORCHD_PROJECTS"):
        return str(paths.projects_dir(env=env)), "env"
    return str(paths.projects_dir(env=env)), ("file" if file_projects_dir(env) is not None else "default")


def show(env=None):
    model, source = worker_model(env=env)
    projects, projects_source = get_value("projects-dir", env)
    return {"file": str(path(env)), "file-source": path_source(env),
            "worker-model": {"value": model, "source": source},
            "projects-dir": {"value": projects, "source": projects_source},
            "review": "always the other vendor than the author (not configurable)"}
