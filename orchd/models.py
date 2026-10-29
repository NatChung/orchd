"""Worker model catalog: alias -> Codex/Claude model id, vendor and tier, from built-in defaults plus config.toml.

    [models.terra]                 # override or add one alias; unset fields keep the built-in default
    id = "gpt-6.1-terra"
    vendor = "codex"               # codex | claude (decides the CLI, the quota bucket and the review pairing)
    quota = "codex"                # codex | claude: which quota bucket the alias draws on (default: its vendor)
    tier = 1                       # 1 default/cheap (auto picks here), 2 escalation, 3 further escalation

Changing a worker's model is a config edit; a new process picks it up (a running MCP server keeps the catalog it read
at start). A bad file raises ValueError naming it, so selection fails closed instead of using half a catalog.
"""
import re
from collections.abc import Mapping

DEFAULTS = {
    "terra": dict(id="gpt-6.1-terra", vendor="codex", quota="codex", tier=1),
    "sonnet": dict(id="claude-sonnet-5-5", vendor="claude", quota="claude", tier=1),
    "sol": dict(id="gpt-6.1-sol", vendor="codex", quota="codex", tier=2),
    "fable": dict(id="claude-fable-5-1", vendor="claude", quota="claude", tier=3),
    "astra": dict(id="gpt-6-astra", vendor="codex", quota="codex", tier=3),
}
VENDORS = ("codex", "claude")
RESERVED = ("auto", "escalate")
# The stored quota providers keep their historical keys: "sol" is the Codex bucket (limit_id codex), "sonnet" the Claude one.
QUOTA_PROVIDER = {"codex": "sol", "claude": "sonnet"}
_ALIAS = re.compile(r"^[a-z][a-z0-9_-]*$")
_cache = {}


def _build(data, file):
    section = data.get("models", {})
    if not isinstance(section, dict):
        raise ValueError(f"{file}: [models] must be a table")
    out = {alias: dict(entry) for alias, entry in DEFAULTS.items()}
    for alias, entry in section.items():
        if not isinstance(entry, dict):
            raise ValueError(f"{file}: [models.{alias}] must be a table")
        if not _ALIAS.match(alias) or alias in RESERVED:
            raise ValueError(f"{file}: [models.{alias}] is not a valid alias (lowercase letters, digits, - _; "
                             f"not {', '.join(RESERVED)})")
        unknown = set(entry) - {"id", "vendor", "quota", "tier"}
        if unknown:
            raise ValueError(f"{file}: [models.{alias}] unknown key {sorted(unknown)[0]!r}; use id, vendor, quota, tier")
        merged = out.setdefault(alias, {})
        if "vendor" in entry and "quota" not in entry and alias in DEFAULTS:
            merged.pop("quota", None)  # a vendor override without a quota follows the new vendor
        merged.update(entry)
    for alias, entry in out.items():
        where = f"{file}: [models.{alias}]"
        if not isinstance(entry.get("id"), str) or not entry["id"].strip():
            raise ValueError(f"{where} needs a non-empty string id")
        if entry.get("vendor") not in VENDORS:
            raise ValueError(f"{where} vendor must be one of {', '.join(VENDORS)}")
        entry.setdefault("quota", entry["vendor"])
        if entry["quota"] not in VENDORS:
            raise ValueError(f"{where} quota must be one of {', '.join(VENDORS)}")
        tier = entry.get("tier")
        if not isinstance(tier, int) or isinstance(tier, bool) or not 1 <= tier <= 9:
            raise ValueError(f"{where} tier must be an integer 1-9")
        if (entry["vendor"] == "codex") != entry["id"].startswith("gpt-"):
            raise ValueError(f"{where} id {entry['id']!r} does not match vendor {entry['vendor']!r} "
                             "(codex ids start with gpt-)")
    if not any(e["tier"] == 1 for e in out.values()):
        raise ValueError(f"{file}: at least one model must be tier 1")
    return out


def catalog(env=None):
    """{alias: {id, vendor, quota, tier}} in defaults-then-config order. Re-read when the file changes."""
    from . import config
    file = config.path(env)
    try:
        stat = file.stat()
        stamp = (str(file), stat.st_mtime_ns, stat.st_size)
    except OSError:
        stamp = (str(file), None, None)
    if _cache.get("stamp") == stamp:
        return _cache["value"]
    value = _build(config._read(env)[1], file)
    _cache.update(stamp=stamp, value=value)
    return value


class WorkerModels(Mapping):
    """alias -> model id, always the current catalog (what runtime.WORKER_MODELS is)."""

    def __getitem__(self, alias):
        return catalog()[alias]["id"]

    def __iter__(self):
        return iter(catalog())

    def __len__(self):
        return len(catalog())

    def __repr__(self):
        return repr({a: e["id"] for a, e in catalog().items()})


def alias_for(model, env=None):
    """The alias for an alias or a full model id; None when unknown."""
    cat = catalog(env)
    if model in cat:
        return model
    return next((a for a, e in cat.items() if e["id"] == model), None)


def vendor_of(model, env=None):
    """codex | claude for an alias or id; ids outside the catalog follow the gpt- prefix rule."""
    alias = alias_for(model, env)
    if alias:
        return catalog(env)[alias]["vendor"]
    return "codex" if isinstance(model, str) and model.startswith("gpt-") else "claude"


def quota_provider(alias, env=None):
    """The stored quota key (sol = Codex bucket, sonnet = Claude bucket) an alias draws on, from its quota field."""
    return QUOTA_PROVIDER[catalog(env)[alias]["quota"]]


def quota_provider_for(model, env=None):
    """Resolve stored ids through the catalog; historical ids retain their vendor's bucket."""
    alias = alias_for(model, env)
    return quota_provider(alias, env) if alias else QUOTA_PROVIDER[vendor_of(model, env)]


def capacity_fallback(model, env=None):
    """Other quota bucket, preferring the same tier, then nearest/lower tier and catalog order."""
    cat = catalog(env)
    alias = alias_for(model, env)
    tier = cat[alias]["tier"] if alias else 1
    provider = quota_provider_for(model, env)
    others = [(abs(e["tier"] - tier), e["tier"], i, a) for i, (a, e) in enumerate(cat.items())
              if quota_provider(a, env) != provider]
    return min(others)[3] if others else None


def default_alias(vendor, env=None):
    """The tier-1 alias auto uses for a vendor (first in catalog order), or None."""
    return next((a for a, e in catalog(env).items() if e["vendor"] == vendor and e["tier"] == 1), None)


def pair(model, env=None):
    """Cross-vendor reviewer/fallback alias: the other vendor's alias at the same tier, else the nearest tier
    (ties go to the lower tier, then catalog order). None if the other vendor has no model."""
    cat = catalog(env)
    alias = alias_for(model, env)
    vendor = vendor_of(model, env)
    tier = cat[alias]["tier"] if alias else 1
    others = [(abs(e["tier"] - tier), e["tier"], i, a) for i, (a, e) in enumerate(cat.items()) if e["vendor"] != vendor]
    return min(others)[3] if others else None


def escalate(model, env=None):
    """Next-tier alias for a worker that failed the task: the lowest tier above its own, preferring the other
    vendor. Raises ValueError when it is already on the top tier."""
    cat = catalog(env)
    alias = alias_for(model, env)
    if alias is None:
        raise ValueError(f"cannot escalate unknown model {model!r}")
    higher = [(e["tier"], e["vendor"] == cat[alias]["vendor"], i, a) for i, (a, e) in enumerate(cat.items())
              if e["tier"] > cat[alias]["tier"]]
    if not higher:
        raise ValueError(f"{alias} is already on the top tier ({cat[alias]['tier']}); choose a model explicitly")
    return min(higher)[3]
