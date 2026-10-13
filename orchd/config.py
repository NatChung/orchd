"""Personal orchd settings: ~/.config/orchd/config.toml (ORCHD_CONFIG_DIR), next to init.toml.

    [worker]
    model = "sol"        # sol | sonnet; factory default sonnet

Worker model precedence: dispatch's explicit model (flag) > ORCHD_WORKER_MODEL (env) > config file > default.
Every layer is validated; a bad value raises ValueError naming its source instead of falling through.
Review always uses the other vendor than the author, so it has no setting.
"""
import os
from pathlib import Path

from . import paths
from .runtime import WORKER_MODELS

try:
    import tomllib
except ImportError:  # Python < 3.11
    tomllib = None

FACTORY_WORKER_MODEL = "sonnet"
KEYS = {"worker-model": ("worker", "model")}


def path(env=None):
    env = os.environ if env is None else env
    if env.get("ORCHD_CONFIG_FILE"):  # exact file; lets tests avoid the real one without moving gh-accounts.json
        return Path(env["ORCHD_CONFIG_FILE"])
    return paths.config_dir(env=env) / "config.toml"


def _load(env=None):
    file = path(env)
    if not file.is_file():
        return {}
    if tomllib is None:
        raise ValueError(f"{file} needs Python 3.11+ (tomllib) to read")
    try:
        return tomllib.loads(file.read_text())
    except tomllib.TOMLDecodeError as error:
        raise ValueError(f"{file}: invalid TOML: {error}") from None


def _check(value, source):
    if not isinstance(value, str):
        raise ValueError(f"{source}: worker model must be a string, one of {', '.join(WORKER_MODELS)}")
    if value not in WORKER_MODELS:
        raise ValueError(f"{source}: unknown worker model {value!r}; use one of {', '.join(WORKER_MODELS)}")
    return value


def worker_model(explicit=None, env=None):
    """(model alias, source) with source one of flag, env, file, default."""
    env = os.environ if env is None else env
    if explicit:
        return _check(explicit, "flag (dispatch model)"), "flag"
    if env.get("ORCHD_WORKER_MODEL"):
        return _check(env["ORCHD_WORKER_MODEL"], "ORCHD_WORKER_MODEL"), "env"
    section = _load(env).get("worker")
    if isinstance(section, dict) and "model" in section:
        return _check(section["model"], str(path(env))), "file"
    return FACTORY_WORKER_MODEL, "default"


def _scalar(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, list):
        return "[" + ", ".join(_scalar(x) for x in v) + "]"
    if isinstance(v, str):
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
    raise ValueError(f"cannot write {type(v).__name__} to config.toml")


def _dump(data):
    """Minimal TOML writer for what this file holds: top-level scalars plus one level of tables."""
    top = [f"{k} = {_scalar(v)}" for k, v in data.items() if not isinstance(v, dict)]
    blocks = ["\n".join(top)] if top else []
    for name, table in data.items():
        if isinstance(table, dict) and table:
            blocks.append(f"[{name}]\n" + "\n".join(f"{k} = {_scalar(v)}" for k, v in table.items()))
    return "\n\n".join(blocks) + "\n" if blocks else ""


def _save(data, env=None):
    file = path(env)
    file.parent.mkdir(parents=True, exist_ok=True)
    tmp = file.with_name(file.name + ".tmp")
    tmp.write_text(_dump(data))
    os.replace(tmp, file)


def _key(key):
    if key not in KEYS:
        raise ValueError(f"unknown config key {key!r}; use one of {', '.join(KEYS)}")
    return KEYS[key]


def set_value(key, value, env=None):
    table, name = _key(key)
    _check(value, "value")
    data = _load(env)
    if not isinstance(data.get(table), dict):
        data[table] = {}
    data[table][name] = value
    _save(data, env)


def unset_value(key, env=None):
    table, name = _key(key)
    data = _load(env)
    if isinstance(data.get(table), dict):
        data[table].pop(name, None)
        if not data[table]:
            del data[table]
        _save(data, env)


def show(env=None):
    model, source = worker_model(env=env)
    return {"file": str(path(env)), "worker-model": {"value": model, "source": source},
            "review": "always the other vendor than the author (not configurable)"}
