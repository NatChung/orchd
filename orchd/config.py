"""Personal orchd settings: ~/.config/orchd/config.toml (ORCHD_CONFIG_DIR), next to init.toml.

    [worker]
    model = "sol"        # sol | sonnet; factory default sonnet

Worker model precedence: dispatch's explicit model (flag) > ORCHD_WORKER_MODEL (env) > config file > default.
Every layer is validated even when a higher one wins; a bad value raises ValueError naming its source.
Only None (flag) and an empty ORCHD_WORKER_MODEL count as "not given". A wrong-shaped file fails closed.
set/unset edit the one line in place and refuse (writing nothing) unless the re-parsed result equals the old data
apart from worker.model, so nothing else in the file is rewritten.
ORCHD_CONFIG_FILE points at an exact file; it is a test/advanced hook and outranks ORCHD_CONFIG_DIR.
Review always uses the other vendor than the author, so it has no setting.
"""
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
KEYS = {"worker-model": ("worker", "model")}
_TABLE = re.compile(r"^\s*\[\s*worker\s*\]\s*(#.*)?$")
_ANY_TABLE = re.compile(r"^\s*\[")
_MODEL = re.compile(r"^\s*model\s*=")


def path(env=None):
    env = os.environ if env is None else env
    if env.get("ORCHD_CONFIG_FILE"):
        return Path(env["ORCHD_CONFIG_FILE"])
    return paths.config_dir(env=env) / "config.toml"


def path_source(env=None):
    env = os.environ if env is None else env
    if env.get("ORCHD_CONFIG_FILE"):
        return "ORCHD_CONFIG_FILE"
    return "ORCHD_CONFIG_DIR" if env.get("ORCHD_CONFIG_DIR") else "default"


def _read(env=None):
    """(text, data); ("", {}) when the file is absent."""
    file = path(env)
    if not file.exists():
        return "", {}
    if not file.is_file():
        raise ValueError(f"{file}: not a file")
    if tomllib is None:
        raise ValueError(f"{file} needs Python 3.11+ (tomllib) to read")
    text = file.read_text()
    try:
        return text, tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise ValueError(f"{file}: invalid TOML: {error}") from None


def _check(value, source):
    if not isinstance(value, str):
        raise ValueError(f"{source}: worker model must be a string, one of {', '.join(WORKER_MODELS)}")
    if value not in WORKER_MODELS:
        raise ValueError(f"{source}: unknown worker model {value!r}; use one of {', '.join(WORKER_MODELS)}")
    return value


def _file_model(data, file, validate=True):
    """The file's worker.model (validated unless told otherwise) or None; a wrong-shaped file raises."""
    if "worker" not in data:
        return None
    section = data["worker"]
    if not isinstance(section, dict):
        raise ValueError(f"{file}: [worker] must be a table, got {type(section).__name__}")
    if "model" not in section:
        return None
    return _check(section["model"], str(file)) if validate else section["model"]


def worker_model(explicit=None, env=None):
    """(model alias, source) with source one of flag, env, file, default."""
    env = os.environ if env is None else env
    flag = None if explicit is None else _check(explicit, "flag (dispatch model)")
    raw = env.get("ORCHD_WORKER_MODEL")
    from_env = _check(raw, "ORCHD_WORKER_MODEL") if raw else None
    from_file = _file_model(_read(env)[1], path(env))
    for value, source in ((flag, "flag"), (from_env, "env"), (from_file, "file")):
        if value is not None:
            return value, source
    return FACTORY_WORKER_MODEL, "default"


def _edit(text, value):
    """Set (str) or remove (None) worker.model by editing lines in place."""
    lines = text.splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines) if _TABLE.match(line)), None)
    new = None if value is None else f'model = "{value}"\n'
    if start is None:
        if new is None:
            return text
        sep = "" if not text else ("\n" if text.endswith("\n") else "\n\n")
        return text + sep + "[worker]\n" + new
    end = next((i for i in range(start + 1, len(lines)) if _ANY_TABLE.match(lines[i])), len(lines))
    at = next((i for i in range(start + 1, end) if _MODEL.match(lines[i])), None)
    if at is None:
        if new is not None:
            lines.insert(start + 1, new)
    elif new is None:
        del lines[at]
    else:
        lines[at] = new
    return "".join(lines)


def _save(old_text, old_data, value, env=None):
    file = path(env)
    new_text = _edit(old_text, value)
    expected = {k: (dict(v) if k == "worker" and isinstance(v, dict) else v) for k, v in old_data.items()}
    if value is not None:
        expected.setdefault("worker", {})["model"] = value
    elif isinstance(expected.get("worker"), dict):
        expected["worker"].pop("model", None)
    try:
        ok = tomllib.loads(new_text) == expected
    except tomllib.TOMLDecodeError:
        ok = False
    if not ok:
        raise ValueError(f"{file}: cannot edit worker.model safely here (dotted or inline form?); "
                         "edit the file by hand. Nothing was written")
    if new_text == old_text:
        return
    file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=file.parent, prefix=file.name + ".", suffix=".tmp")  # 0600, unique per writer
    try:
        with os.fdopen(fd, "w") as handle:
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
    return KEYS[key]


def set_value(key, value, env=None):
    _key(key)
    _check(value, "value")
    text, data = _read(env)
    _file_model(data, path(env), validate=False)  # a bad current value must stay fixable
    _save(text, data, value, env)


def unset_value(key, env=None):
    _key(key)
    text, data = _read(env)
    _file_model(data, path(env), validate=False)  # a bad current value must stay fixable
    _save(text, data, None, env)


def show(env=None):
    model, source = worker_model(env=env)
    return {"file": str(path(env)), "file-source": path_source(env),
            "worker-model": {"value": model, "source": source},
            "review": "always the other vendor than the author (not configurable)"}
