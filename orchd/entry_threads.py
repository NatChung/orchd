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


# --- question / task readers: full text, identity by supersedes chain + reply_to, paged by cursor ---
import base64
import json
from datetime import datetime, timezone

SCHEMA_VERSION = 1
EXIT_CODES = dict(ok=0, not_found=3, error=4, partial=5)  # 2 is argparse / bad cursor


class CursorError(ValueError):
    """Bad cursor or limit: a usage error, not a read failure."""


def _iso(ts):
    return None if ts is None else datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def _limit(value, name):
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 100:
        raise CursorError(f"{name} must be between 1 and 100")
    return value


def _encode(**fields):
    return base64.urlsafe_b64encode(json.dumps(fields, separators=(",", ":")).encode()).decode()


def _decode(cursor, *keys):
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        if set(data) != set(keys) or not all(isinstance(data[k], (int, float)) for k in keys):
            raise ValueError
        return [data[k] for k in keys]
    except (ValueError, TypeError, AttributeError):
        raise CursorError("invalid cursor") from None


def _envelope(status, threads, **extra):
    return dict(schema_version=SCHEMA_VERSION, read_at=_iso(datetime.now(timezone.utc).timestamp()),
                status=status, threads=threads, **extra)


def _row(con, message_id, entry_id):
    return con.execute("SELECT * FROM entry_messages WHERE id=? AND entry_id=? AND kind='question'",
                       (message_id, entry_id)).fetchone()


def _chain(con, question, entry_id):
    """Every version of the question (oldest first) and the ids a link pointed to that no longer exist."""
    keys = question.keys()
    if "supersedes" not in keys:
        return [question], []
    chain, missing, seen = [question], [], {question["id"]}
    for field, front in (("supersedes", True), ("superseded_by", False)):
        cur = question
        while cur[field] is not None:
            if cur[field] in seen:
                break
            nxt = _row(con, cur[field], entry_id)
            if nxt is None:
                missing.append(cur[field])
                break
            seen.add(nxt["id"])
            chain.insert(0, nxt) if front else chain.append(nxt)
            cur = nxt
    return chain, missing


def _thread(con, head, entry_id, limit, cursor):
    chain, missing = _chain(con, head, entry_id)
    head = chain[-1]
    ids = [q["id"] for q in chain]
    marks = ",".join("?" * len(ids))
    where, args = f"entry_id=? AND (id IN ({marks}) OR (reply_to IN ({marks}) AND kind='reply'))", [entry_id, *ids, *ids]
    latest = con.execute(f"SELECT direction FROM entry_messages WHERE {where} ORDER BY created_at DESC,id DESC LIMIT 1", args).fetchone()
    page = where
    if cursor is not None:
        page += " AND (created_at,id) < (?,?)"
        args = args + _decode(cursor, "t", "i")
    rows = con.execute(f"SELECT * FROM entry_messages WHERE {page} ORDER BY created_at DESC,id DESC LIMIT ?",
                       args + [limit + 1]).fetchall()
    more, rows = len(rows) > limit, rows[:limit]
    messages = [dict(id=r["id"], question_id=r["reply_to"] if r["kind"] == "reply" else r["id"], task_id=r["task_id"],
                     reply_to=r["reply_to"], supersedes=r["supersedes"] if "supersedes" in r.keys() else None,
                     superseded_by=r["superseded_by"] if "superseded_by" in r.keys() else None,
                     direction="orch→nat" if r["direction"] == "out" else "nat→orch", kind=r["kind"],
                     who="orch" if r["direction"] == "out" else "nat", at=_iso(r["created_at"]), text=r["body"])
                for r in rows]
    root = chain[0]
    keys = head.keys()
    return dict(question_id=head["id"], task_id=head["task_id"], issue_ref=head["issue_ref"] if "issue_ref" in keys else None,
                question_state=head["question_state"], revisit_at=_iso(head["revisit_at"]) if "revisit_at" in keys else None,
                status=("none" if latest is None else "waiting_nat" if latest["direction"] == "out" else "waiting_orch"),
                root_question=dict(id=root["id"], body=root["body"]), question_chain=ids, missing_history=missing,
                messages=messages, complete=not missing, has_more=more,
                next_cursor=_encode(t=rows[-1]["created_at"], i=rows[-1]["id"]) if more else None)


def _head(con, question, entry_id):
    chain, _ = _chain(con, question, entry_id)
    return chain[-1]


def read_question(path, question_id, *, entry_id="desktop", limit=3, cursor=None):
    """One question's full thread. status: ok | partial (a chain link is missing) | not_found."""
    _limit(limit, "limit")
    with stats.open_snapshot(path) as snap:
        con = snap.con
        con.execute("PRAGMA query_only=ON")
        question = _row(con, question_id, entry_id)
        if question is None:
            return _envelope("not_found", [], question_id=question_id)
        thread = _thread(con, question, entry_id, limit, cursor)
    return _envelope("partial" if thread["missing_history"] else "ok", [thread])


def read_task(path, task_id, *, entry_id="desktop", per_question_limit=3, thread_limit=20, cursor=None):
    """Every question of a task (latest version of each), each with its own thread; paged by question id."""
    _limit(per_question_limit, "per-question-limit")
    _limit(thread_limit, "thread-limit")
    with stats.open_snapshot(path) as snap:
        con = snap.con
        con.execute("PRAGMA query_only=ON")
        columns = {r["name"] for r in con.execute("PRAGMA table_info(entry_messages)")}
        tail = " AND superseded_by IS NULL" if "superseded_by" in columns else ""
        sql, args = "SELECT * FROM entry_messages WHERE entry_id=? AND task_id=? AND kind='question'" + tail, [entry_id, task_id]
        if cursor is not None:
            sql, args = sql + " AND id < ?", args + _decode(cursor, "q")
        rows = con.execute(sql + " ORDER BY id DESC LIMIT ?", args + [thread_limit + 1]).fetchall()
        more, rows = len(rows) > thread_limit, rows[:thread_limit]
        if not rows and cursor is None:
            return _envelope("not_found", [], task_id=task_id, has_more=False, next_cursor=None)
        threads = [_thread(con, r, entry_id, per_question_limit, None) for r in rows]
    status = "partial" if any(t["missing_history"] for t in threads) else "ok"
    return _envelope(status, threads, task_id=task_id, has_more=more, next_cursor=_encode(q=rows[-1]["id"]) if more else None)
