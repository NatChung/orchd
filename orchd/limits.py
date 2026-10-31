"""Shared output bounds for list-style tool results (MCP and CLI): per-field text caps and counted pages."""
import json

TEXT_CAP = 500        # characters kept per free-text field in a list row
DEFAULT_LIMIT = 50    # rows per list_open page
MAX_PAGE_BYTES = 60_000  # bytes of the final wire reply per list_open / intake_pending page
ID_MARGIN = 32           # room for the JSON-RPC id, which the page builders do not know


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


def tool_text(data):
    """The text a tool result carries: what the client reads, and what the byte budget is measured on."""
    return json.dumps(data, ensure_ascii=False, indent=1)


def wire_bytes(data):
    """Bytes of the one-line JSON-RPC reply carrying `data` (the tool text is escaped a second time inside it).
    The id is unknown here, so a fixed margin stands in for it."""
    reply = {"jsonrpc": "2.0", "id": 0, "result": {"content": [{"type": "text", "text": tool_text(data)}]}}
    return len(json.dumps(reply, ensure_ascii=False).encode()) + ID_MARGIN


def cap_strings(value, cap, floor=64):
    """Copy of value with every string longer than max(cap, floor) cut to cap characters plus a marker.
    Short strings (ids, statuses, timestamps) are never touched."""
    if isinstance(value, str):
        return cap_text(value, cap) if len(value) > max(cap, floor) else value
    if isinstance(value, dict):
        return {k: cap_strings(v, cap, floor) for k, v in value.items()}
    if isinstance(value, list):
        return [cap_strings(v, cap, floor) for v in value]
    return value


def fit_wire(items, build, max_bytes=MAX_PAGE_BYTES):
    """Longest prefix of items whose final reply, build(prefix), fits max_bytes on the wire.

    A first item that is too big alone is shortened (long text fields cut, with a marker) rather than dropped,
    so a paging cursor always advances. At least one item is returned when items is non-empty."""
    if not items:
        return []
    fits = lambda n: wire_bytes(build(items[:n])) <= max_bytes
    low, high = 0, len(items)
    while low < high:  # largest n that fits; fits() only gets harder as n grows
        mid = (low + high + 1) // 2
        low, high = (mid, high) if fits(mid) else (low, mid - 1)
    if low:
        return items[:low]
    first = items[0]
    for cap in (TEXT_CAP, 200, 100, 50):
        first = cap_strings(items[0], cap)
        if wire_bytes(build([first])) <= max_bytes:
            break
    return [first]
