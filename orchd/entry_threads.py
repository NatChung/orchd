"""Local-only entry threads. SELECTs on a private DB/WAL copy; no runtime or migrations."""
import base64
import binascii
import datetime
import json
import sqlite3
import time

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


# --- Question/task threads (#44): full text, per-question identity, keyset pagination ------------------------

QUESTION_SCHEMA_VERSION = 1
# Distinct exit codes so a caller never mistakes a failure for "no history".
EXIT_CODES = dict(ok=0, invalid_request=2, not_found=3, incomplete=4, read_error=5)
MAX_LIMIT = 100
DEFAULT_THREAD_LIMIT = 20
_OPTIONAL = ("issue_ref", "supersedes", "superseded_by", "revisit_at", "version")


class QueryError(ValueError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def _utc(value):
    if value is None:
        return None
    return datetime.datetime.fromtimestamp(value, datetime.timezone.utc).isoformat(
        timespec="microseconds").replace("+00:00", "Z")


def _check_limit(name, value):
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= MAX_LIMIT:
        raise QueryError("invalid_request", f"{name} must be between 1 and {MAX_LIMIT}")
    return value


def encode_cursor(**fields):
    raw = json.dumps(dict(v=1, **fields), separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor, kind):
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        data = json.loads(raw.decode("utf-8"))
        valid = (isinstance(data, dict) and data.get("v") == 1 and data.get("k") == kind
                 and isinstance(data.get("at"), (int, float)) and not isinstance(data.get("at"), bool)
                 and isinstance(data.get("id"), int) and not isinstance(data.get("id"), bool))
    except (ValueError, TypeError, UnicodeDecodeError, binascii.Error):
        valid = False
    if not valid:
        raise QueryError("invalid_request", f"cursor is not a valid {kind} cursor")
    return data


class _Reader:
    def __init__(self, con, entry_id):
        self.con, self.entry_id = con, entry_id
        columns = {r["name"] for r in con.execute("PRAGMA table_info(entry_messages)")}
        if not columns:
            raise QueryError("read_error", "entry_messages table is missing")
        self.select = ",".join(["id", "entry_id", "direction", "kind", "body", "task_id", "reply_to",
                                "question_state", "created_at"]
                               + [c if c in columns else f"NULL AS {c}" for c in _OPTIONAL])

    def question(self, message_id):
        row = self.con.execute(f"SELECT {self.select} FROM entry_messages WHERE id=?", (message_id,)).fetchone()
        return row if row is not None and row["kind"] == "question" and row["entry_id"] == self.entry_id else None

    def _walk(self, start, link, back_link, missing, seen, issues):
        found, node = [], start
        while node[link] is not None:
            other = self.question(node[link])
            if other is None:
                issues.append(dict(problem=missing, question_id=node["id"], missing_id=node[link]))
                break
            if other[back_link] != node["id"]:
                issues.append(dict(problem="inconsistent_supersede_link", question_id=node["id"], other_id=other["id"]))
            if other["id"] in seen:
                issues.append(dict(problem="supersede_cycle", question_id=other["id"]))
                break
            seen.add(other["id"])
            found.append(other)
            node = other
        return found

    def chain(self, start):
        """All versions linked to `start` through supersedes/superseded_by, oldest first, plus link problems."""
        issues, seen = [], {start["id"]}
        back = self._walk(start, "supersedes", "superseded_by", "missing_superseded_question", seen, issues)
        forward = self._walk(start, "superseded_by", "supersedes", "missing_superseding_question", seen, issues)
        versions = back[::-1] + [start] + forward
        ids = [q["id"] for q in versions]
        replied = {r["reply_to"] for r in self.con.execute(
            f"SELECT DISTINCT reply_to FROM entry_messages WHERE entry_id=? AND reply_to IN ({','.join('?' * len(ids))})",
            (self.entry_id, *ids))}
        issues += [dict(problem="answered_without_reply", question_id=q["id"]) for q in versions
                   if q["question_state"] == "answered" and q["id"] not in replied]
        return versions, issues

    def messages(self, ids, limit, after=None):
        """Chain questions plus every message replying to one of them; newest first, id breaks time ties."""
        marks = ",".join("?" * len(ids))
        sql = (f"SELECT {self.select} FROM entry_messages WHERE entry_id=? "
               f"AND ((kind='question' AND id IN ({marks})) OR reply_to IN ({marks}))")
        params = [self.entry_id, *ids, *ids]
        if after is not None:
            sql += " AND (created_at<? OR (created_at=? AND id<?))"
            params += [after["at"], after["at"], after["id"]]
        return self.con.execute(sql + " ORDER BY created_at DESC,id DESC LIMIT ?", (*params, limit)).fetchall()


def _message(row):
    question = row["kind"] == "question"
    return dict(id=row["id"], question_id=row["id"] if question else row["reply_to"], task_id=row["task_id"],
                reply_to=row["reply_to"], supersedes=row["supersedes"] if question else None,
                superseded_by=row["superseded_by"] if question else None,
                direction="orch→nat" if row["direction"] == "out" else "nat→orch", kind=row["kind"],
                who="orch" if row["direction"] == "out" else "nat", at=_utc(row["created_at"]), text=row["body"])


def _thread(reader, versions, issues, limit, cursor=None, requested=None):
    head = versions[-1]
    ids = [q["id"] for q in versions]
    after = None
    if cursor is not None:
        after = decode_cursor(cursor, "messages")
        if after.get("q") not in ids:
            raise QueryError("invalid_request", "cursor belongs to another question")
    rows = reader.messages(ids, limit + 1, after)
    page, has_more = rows[:limit], len(rows) > limit
    latest = reader.messages(ids, 1)[0]  # the head question itself guarantees one row
    task_id = head["task_id"]
    if task_id is None:  # revisions do not inherit task links; keep the chain's most recent one
        task_id = next((q["task_id"] for q in reversed(versions) if q["task_id"] is not None), None)
    return dict(
        question_id=head["id"], requested_question_id=requested, task_id=task_id, issue_ref=head["issue_ref"],
        question_state=head["question_state"], revisit_at=_utc(head["revisit_at"]),
        version=head["version"] if head["version"] is not None else 1, versions=ids,
        status="waiting_nat" if latest["direction"] == "out" else "waiting_orch",
        root_question=dict(id=head["id"], body=head["body"], at=_utc(head["created_at"])),
        messages=[_message(r) for r in page],
        complete=not issues, history_issues=issues, has_more=has_more,
        next_cursor=encode_cursor(k="messages", q=head["id"], at=page[-1]["created_at"], id=page[-1]["id"])
        if has_more else None,
        _last=(latest["created_at"], latest["id"]))


def _query(con, entry_id, question_id, task_id, limit, per_question_limit, cursor):
    reader = _Reader(con, entry_id)
    if question_id is not None:
        start = reader.question(question_id)
        if start is None:
            raise QueryError("not_found", f"question {question_id} is not a question of entry {entry_id}")
        versions, issues = reader.chain(start)
        return dict(threads=[_thread(reader, versions, issues, limit, cursor, requested=question_id)])
    known = con.execute("SELECT 1 FROM tasks WHERE id=?", (task_id,)).fetchone() is not None
    rows = con.execute(f"SELECT {reader.select} FROM entry_messages WHERE entry_id=? AND kind='question' "
                       "AND task_id=? ORDER BY id", (entry_id, task_id)).fetchall()
    if not rows and not known:
        raise QueryError("not_found", f"task {task_id} is unknown and has no questions on entry {entry_id}")
    threads, covered = [], set()
    for row in rows:
        if row["id"] in covered:
            continue
        versions, issues = reader.chain(row)
        covered.update(q["id"] for q in versions)
        threads.append(_thread(reader, versions, issues, per_question_limit))
    threads.sort(key=lambda t: t["_last"], reverse=True)
    total = len(threads)
    if cursor is not None:
        after = decode_cursor(cursor, "threads")
        if after.get("task") != task_id:
            raise QueryError("invalid_request", "cursor belongs to another task")
        threads = [t for t in threads if t["_last"] < (after["at"], after["id"])]
    page, has_more = threads[:limit], len(threads) > limit
    return dict(threads=page, thread_page=dict(
        limit=limit, returned=len(page), total=total, has_more=has_more,
        next_cursor=encode_cursor(k="threads", task=task_id, at=page[-1]["_last"][0], id=page[-1]["_last"][1])
        if has_more else None))


def read_questions(path, *, question_id=None, task_id=None, entry_id="desktop", limit=None,
                   per_question_limit=None, cursor=None):
    """Read-only question threads. Returns (payload, exit code); payload["status"] names the outcome
    and is never "ok" with an empty list standing in for a failed or partial read."""
    selector = (dict(type="question", question_id=question_id) if question_id is not None
                else dict(type="task", task_id=task_id))
    payload = dict(schema_version=QUESTION_SCHEMA_VERSION, status="ok", read_at=_utc(time.time()),
                   entry_id=entry_id, selector=selector, threads=[])
    try:
        if (question_id is None) == (task_id is None):
            raise QueryError("invalid_request", "exactly one of question id or task id is required")
        if cursor is not None and (not isinstance(cursor, str) or not cursor):
            raise QueryError("invalid_request", "cursor must be a non-empty string")
        if question_id is not None:
            if per_question_limit is not None:
                raise QueryError("invalid_request", "--per-question-limit applies only to --task-id; use --limit")
            if not isinstance(question_id, int) or isinstance(question_id, bool) or question_id < 1:
                raise QueryError("invalid_request", "question id must be a positive integer")
            limit = _check_limit("limit", 3 if limit is None else limit)
            selector.update(limit=limit, cursor=cursor)
        else:
            if not isinstance(task_id, str) or not task_id.strip():
                raise QueryError("invalid_request", "task id must be non-empty")
            limit = _check_limit("limit", DEFAULT_THREAD_LIMIT if limit is None else limit)
            per_question_limit = _check_limit("per-question-limit",
                                              3 if per_question_limit is None else per_question_limit)
            selector.update(limit=limit, per_question_limit=per_question_limit, cursor=cursor)
        with stats.open_snapshot(path) as snap:
            snap.con.execute("PRAGMA query_only=ON")
            payload.update(_query(snap.con, entry_id, question_id, task_id, limit, per_question_limit, cursor))
        for thread in payload["threads"]:
            del thread["_last"]
        if not all(t["complete"] for t in payload["threads"]):
            payload["status"] = "incomplete"
    except QueryError as error:
        payload.update(status=error.status, error=str(error), threads=[])
        payload.pop("thread_page", None)
    except (OSError, sqlite3.Error, stats.SnapshotError) as error:
        payload.update(status="read_error", error=f"{type(error).__name__}: {error}", threads=[])
        payload.pop("thread_page", None)
    payload["complete"] = payload["status"] == "ok"
    return payload, EXIT_CODES[payload["status"]]
