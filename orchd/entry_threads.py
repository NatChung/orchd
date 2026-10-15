"""Local-only entry body summaries. SELECTs on a private DB/WAL copy; no runtime or migrations."""
from . import issue_links, stats


def read(path, issue_refs, *, entry_id="desktop", limit=3):
    """Return latest first, with deterministic timestamp/id ties, separately for each ref."""
    refs = list(dict.fromkeys(issue_refs))
    if not refs:
        raise ValueError("at least one issue ref is required")
    for ref in refs:
        issue_links.validate_ref(ref)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    result = dict(schema_version=1, entry_id=entry_id, threads={})
    with stats.open_snapshot(path) as snap:
        con = snap.con
        con.execute("PRAGMA query_only=ON")
        columns = {r["name"] for r in con.execute("PRAGMA table_info(entry_messages)")}
        for ref in refs:
            rows = con.execute(
                "SELECT id,created_at,direction,kind,body FROM entry_messages "
                "WHERE entry_id=? AND issue_ref=? ORDER BY created_at DESC,id DESC LIMIT ?",
                (entry_id, ref, limit)).fetchall() if "issue_ref" in columns else []
            messages = [dict(message_id=r["id"], at=r["created_at"],
                             who="orch" if r["direction"] == "out" else "nat",
                             direction="orch→nat" if r["direction"] == "out" else "nat→orch",
                             kind=r["kind"], text=r["body"][:200], full_bytes=len(r["body"].encode("utf-8")))
                        for r in rows]
            status = ("waiting_nat" if messages[0]["who"] == "orch" else "waiting_orch") if messages else "none"
            result["threads"][ref] = dict(status=status, messages=messages)
    return result
