"""Shared output bounds for list-style tool results (MCP and CLI): per-field text caps and counted pages."""
import json

TEXT_CAP = 500        # characters kept per free-text field in a list row
DEFAULT_LIMIT = 50    # rows per list_open page
MAX_PAGE_BYTES = 60_000  # serialized bytes per intake_pending page


def cap_text(value, cap=TEXT_CAP):
    """Truncate a string with a visible marker saying how much was dropped; other types pass through."""
    if not isinstance(value, str) or len(value) <= cap:
        return value
    return f"{value[:cap]}…[truncated {len(value) - cap} chars]"


def cap_fields(row, fields, cap=TEXT_CAP):
    """Copy of row with the named text fields capped."""
    return {k: cap_text(v, cap) if k in fields else v for k, v in row.items()}


def page(items, limit=None, *, noun="items", hint=None):
    """Counted page: total, showing, the first `limit` items and, when cut, how to see the rest."""
    limit = DEFAULT_LIMIT if limit is None else limit
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError("limit must be a positive integer")
    shown = items[:limit]
    out = dict(total=len(items), showing=len(shown), truncated=len(shown) < len(items), **{noun: shown})
    if out["truncated"]:
        out["hint"] = hint or f"{len(items) - len(shown)} more not shown; narrow with a filter or raise limit"
    return out


def fit_bytes(items, max_bytes=MAX_PAGE_BYTES):
    """Longest prefix of items whose JSON stays within max_bytes (always at least one item)."""
    used, count = 0, 0
    for item in items:
        used += len(json.dumps(item, ensure_ascii=False).encode()) + 1
        if count and used > max_bytes:
            break
        count += 1
    return items[:count]
