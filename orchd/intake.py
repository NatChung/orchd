"""Local intake contract: durable queue for polled source events, plus reply drafts, approvals and send receipts.

Producers (source adapters) only call the `orchd intake` CLI; nothing here reads or writes sources. Third-party
content is stored as short metadata (`summary`), marked data, and never becomes an Entry message, a task or a
permission. Every state change appends one `intake_log` row; its max seq is the snapshot `revision`.
Contract reference: docs/intake.md. Frozen fixtures: tests/fixtures/intake/.
"""
import hashlib
import json
import math
import re
import sqlite3
import time
import uuid
from pathlib import Path

from . import limits, store

VERSION = 1
PROVIDERS = ("email", "slack", "line")
CLASSIFICATIONS = ("A", "B", "C", "D")
LEVELS = ("L0", "L1", "L2")
ACTORS = ("orch", "operator")
SUMMARY_MAX = 1000
BODY_MAX = 20000
RETENTION_SECONDS = 7 * 86400
WARN_BEFORE_SECONDS = 2 * 86400
# L1 fixed low-risk auto-send is a reserved, disabled capability: no code path reads a switch to enable it.
AUTO_SEND = dict(enabled=False, reason="disabled by default; every outbound draft needs explicit approval")

EVENT_FIELDS = {"version", "provider", "account", "company", "event_id", "source_ref", "thread_ref",
                "source_time", "summary", "owner_orch"}
REQUIRED = ("version", "provider", "account", "event_id", "source_ref", "source_time", "summary")
ISSUE_REF = re.compile(r"(?:github:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?/[A-Za-z0-9_.-]+#[1-9][0-9]*"
                       r"|jira:[A-Z][A-Z0-9_]*-[1-9][0-9]*)\Z")

ERROR_CODES = ("invalid", "unknown_field", "producer_not_allowed", "account_not_allowed", "unknown_owner",
               "conflict", "not_found", "stale", "expired", "target_mismatch", "invalid_state",
               "already_claimed", "idempotency_conflict", "not_owner")


class IntakeError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        assert code in ERROR_CODES, code
        self.code = code


def failure(error):
    """The JSON a CLI prints for a rejected call. Rejections never write."""
    return dict(status="failed", error_code=error.code, error=str(error))


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _text(value, name, *, limit=None, required=True):
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        raise IntakeError("invalid", f"{name} must be a non-empty string")
    if limit is not None and len(value) > limit:
        raise IntakeError("invalid", f"{name} exceeds {limit} characters")
    return value


def _time(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise IntakeError("invalid", f"{name} must be a positive finite unix timestamp")
    return float(value)


def _log(con, kind, ref):
    con.execute("INSERT INTO intake_log(kind,ref,created_at) VALUES(?,?,?)", (kind, ref, time.time()))


def revision(con):
    return con.execute("SELECT COALESCE(MAX(seq),0) FROM intake_log").fetchone()[0]


def check_producer(producers, name, provider, account):
    """producers: {name: {providers: [...], accounts: [...]}} from config. Unset or unknown = reject."""
    grant = (producers or {}).get(name)
    if not grant:
        raise IntakeError("producer_not_allowed",
                          f"producer {name!r} is not allowed: no [intake.producers.{name}] grant in the orchd "
                          "config (an unset allowlist rejects everything); nothing was written")
    if provider not in grant.get("providers", ()) or account not in grant.get("accounts", ()):
        raise IntakeError("account_not_allowed",
                          f"producer {name!r} is not granted provider {provider!r} account {account!r}; "
                          "nothing was written")


def _normalize(payload):
    if not isinstance(payload, dict):
        raise IntakeError("invalid", "event must be a JSON object")
    extra = sorted(set(payload) - EVENT_FIELDS)
    if extra:  # identity or body claims in the payload are refused, never ignored silently
        raise IntakeError("unknown_field", f"unknown event field(s): {', '.join(extra)}")
    missing = [k for k in REQUIRED if payload.get(k) is None]
    if missing:
        raise IntakeError("invalid", f"missing field(s): {', '.join(missing)}")
    if payload["version"] != VERSION or isinstance(payload["version"], bool):
        raise IntakeError("invalid", f"version must be {VERSION}")
    if payload["provider"] not in PROVIDERS:
        raise IntakeError("invalid", f"provider must be one of {', '.join(PROVIDERS)}")
    event = dict(version=VERSION, provider=payload["provider"],
                 account=_text(payload["account"], "account"),
                 company=_text(payload.get("company", None) or payload["account"], "company"),
                 event_id=_text(payload["event_id"], "event_id"),
                 source_ref=_text(payload["source_ref"], "source_ref"),
                 thread_ref=_text(payload.get("thread_ref"), "thread_ref", required=False),
                 source_time=_time(payload["source_time"], "source_time"),
                 summary=_text(payload["summary"], "summary", limit=SUMMARY_MAX))
    owner = payload.get("owner_orch")
    return event, (_text(owner, "owner_orch") if owner is not None else None)


def _content_hash(event):
    return _sha(json.dumps(event, sort_keys=True, ensure_ascii=False, separators=(",", ":")))


def _check_owner(con, owner):
    row = store.get_orch(con, owner)
    if row is None:
        raise IntakeError("unknown_owner", f"owner_orch {owner!r} is not a registered Orch")
    return row


def _event_view(row):
    keys = ("receipt_id", "version", "provider", "account", "company", "event_id", "source_ref", "thread_ref",
            "source_time", "received_at", "producer", "summary", "owner_orch", "state", "acked_at", "acked_by",
            "classification", "level", "todo_id", "issue_ref", "project")
    view = {k: row[k] for k in keys}
    # Snapshot reads unmigrated v1 databases too; missing additive columns are null.
    view.update({k: row[k] if k in row.keys() else None for k in
                 ("excluded_at", "excluded_by", "exclusion_reason", "exclusion_rule_version")})
    view["id"] = row["id"]
    return view


def _receipt(row, status):
    return dict(status=status, receipt_id=row["receipt_id"], provider=row["provider"], account=row["account"],
                event_id=row["event_id"], received_at=row["received_at"], state=row["state"],
                owner_orch=row["owner_orch"],
                owner_state="assigned" if row["owner_orch"] else "unassigned",
                content_sha256=row["content_sha256"],
                **{k: row[k] for k in ("excluded_at", "excluded_by", "exclusion_reason",
                                      "exclusion_rule_version")})


def submit(con, payload, *, producer, producers):
    """Durable receipt for one normalized event. Same key and content returns the original receipt as
    `duplicate`; same key with different content is `conflict` and changes nothing. Validation and the
    producer grant run before any write."""
    if isinstance(payload, dict) and payload.get("version") == 2:  # evidence + suggestions: intake_bridge
        from . import intake_bridge
        return intake_bridge.submit(con, payload, producer=producer, producers=producers)
    event, owner = _normalize(payload)
    check_producer(producers, producer, event["provider"], event["account"])
    digest = _content_hash(event)
    with store.immediate(con):
        row = con.execute("SELECT * FROM intake_events WHERE provider=? AND account=? AND event_id=?",
                          (event["provider"], event["account"], event["event_id"])).fetchone()
        if row is not None:
            if row["content_sha256"] != digest:
                raise IntakeError("conflict", "event_id already received with different content; not overwritten")
            return _receipt(row, "duplicate")
        if owner is not None:
            _check_owner(con, owner)
        receipt_id = "ir_" + uuid.uuid4().hex[:16]
        con.execute(
            "INSERT INTO intake_events(receipt_id,version,provider,account,company,event_id,source_ref,thread_ref,"
            "source_time,received_at,producer,summary,content_sha256,owner_orch) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (receipt_id, VERSION, event["provider"], event["account"], event["company"], event["event_id"],
             event["source_ref"], event["thread_ref"], event["source_time"], time.time(), producer,
             event["summary"], digest, owner))
        _log(con, "event", receipt_id)
        return _receipt(con.execute("SELECT * FROM intake_events WHERE receipt_id=?", (receipt_id,)).fetchone(),
                        "received")


def _event(con, receipt_id):
    row = con.execute("SELECT * FROM intake_events WHERE receipt_id=?", (receipt_id,)).fetchone()
    if row is None:
        raise IntakeError("not_found", f"unknown receipt {receipt_id}")
    return row


def pending(con, owner=None, limit=100, *, after_id=None, high_water_id=None, cursor=None,
            max_bytes=limits.MAX_PAGE_BYTES):
    """Read a live queue in id order, bounded by a traversal's fixed high-water id.

    Carry next_cursor to keep the boundary and owner. Queue membership is evaluated on each page;
    events bound into a queue behind its cursor are picked up by the next traversal, not lost forever.
    """
    scope = _sha(json.dumps(owner))[:16]
    if cursor is not None:
        if after_id is not None or high_water_id is not None:
            raise IntakeError("invalid", "cursor cannot be combined with after-id or high-water-id")
        try:
            tag, after, high, cursor_scope = cursor.split(":")
            if tag != "p1" or cursor_scope != scope:
                raise ValueError
            after_id, high_water_id = int(after), int(high)
        except (ValueError, AttributeError):
            raise IntakeError("invalid", "invalid cursor or cursor belongs to another owner queue") from None
    after_id = 0 if after_id is None else after_id
    for name, value in (("after-id", after_id), ("high-water-id", high_water_id)):
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            raise IntakeError("invalid", f"{name} must be a non-negative integer")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise IntakeError("invalid", "limit must be a positive integer")
    if high_water_id is None:
        high_water_id = con.execute("SELECT COALESCE(MAX(id),0) FROM intake_events").fetchone()[0]
    if after_id > high_water_id:
        raise IntakeError("invalid", "after-id exceeds high-water-id")
    clause, params = ("owner_orch IS NULL", ()) if owner is None else ("owner_orch=?", (owner,))
    rows = con.execute("SELECT * FROM intake_events WHERE state='received' AND " + clause +
                       " AND id>? AND id<=? ORDER BY id LIMIT ?",
                       (*params, after_id, high_water_id, limit + 1)).fetchall()
    events = limits.fit_bytes([_event_view(r) for r in rows[:limit]], max_bytes)  # a page is bounded in bytes too
    has_more = len(rows) > len(events)
    last_id = events[-1]["id"] if events else after_id
    return dict(owner=owner, events=events, has_more=has_more, high_water_id=high_water_id,
                next_cursor=f"p1:{last_id}:{high_water_id}:{scope}" if has_more else None)


def exclude(con, receipt_ids, *, reason, rule_version, actor):
    """Explicit Orch/Operator decision, never inferred from third-party event content. Atomic batch."""
    reason = _text(reason, "reason", limit=200)
    rule_version = _text(rule_version, "rule-version", limit=200)
    if actor not in ACTORS:
        raise IntakeError("invalid", "actor must be orch or operator")
    if not receipt_ids:
        raise IntakeError("invalid", "at least one receipt is required")
    with store.immediate(con):
        rows = [_event(con, r) for r in dict.fromkeys(receipt_ids)]
        for row in rows:
            if row["state"] == "excluded":
                if (row["exclusion_reason"], row["exclusion_rule_version"], row["excluded_by"]) != \
                        (reason, rule_version, actor):
                    raise IntakeError("conflict", "receipt already excluded with a different decision")
            elif row["state"] != "received":
                raise IntakeError("invalid_state", "only received events can be excluded")
        results = []
        for row in rows:
            if row["state"] == "excluded":
                results.append(_receipt(row, "duplicate"))
                continue
            con.execute("UPDATE intake_events SET state='excluded',excluded_at=?,excluded_by=?,"
                        "exclusion_reason=?,exclusion_rule_version=? WHERE receipt_id=?",
                        (time.time(), actor, reason, rule_version, row["receipt_id"]))
            _log(con, "exclude", row["receipt_id"])
            results.append(_receipt(_event(con, row["receipt_id"]), "excluded"))
        return dict(status="excluded", receipts=results)


def bind_owner(con, receipt_id, owner, *, only_if_unassigned=False):
    """Bind (or, as the Operator, re-bind) a received receipt. only_if_unassigned makes the assignment a
    compare-and-set: the owner IS NULL / state='received' condition is in the UPDATE itself, so a receipt another
    Orch took after the caller looked is reported `skipped` and never overwritten."""
    with store.immediate(con):
        row = _event(con, receipt_id)
        if only_if_unassigned:
            _check_owner(con, owner)
            changed = con.execute("UPDATE intake_events SET owner_orch=? WHERE receipt_id=? AND state='received' "
                                  "AND owner_orch IS NULL", (owner, receipt_id)).rowcount
            if changed != 1:
                return _receipt(_event(con, receipt_id), "skipped")
            _log(con, "bind", receipt_id)
            from . import intake_notify
            intake_notify.rebind(con, receipt_id, None, owner)
            return _receipt(_event(con, receipt_id), "bound")
        if row["state"] == "excluded":
            raise IntakeError("invalid_state", "an excluded event cannot be bound")
        _check_owner(con, owner)
        if row["owner_orch"] == owner:
            return _receipt(row, "unchanged")
        if row["owner_orch"] is not None and row["state"] != "received":
            raise IntakeError("invalid_state", "an acked event keeps its owner")
        con.execute("UPDATE intake_events SET owner_orch=? WHERE receipt_id=?", (owner, receipt_id))
        _log(con, "bind", receipt_id)
        from . import intake_notify
        intake_notify.rebind(con, receipt_id, row["owner_orch"], owner)
        return _receipt(_event(con, receipt_id), "bound")


def _move_received(con, row, new_owner):
    """Inside the caller's write transaction: re-point one received receipt and its notification duty."""
    from . import intake_notify
    moved = con.execute("UPDATE intake_events SET owner_orch=? WHERE receipt_id=? AND state='received' "
                        "AND owner_orch IS ?", (new_owner, row["receipt_id"], row["owner_orch"])).rowcount
    if moved != 1:
        raise IntakeError("conflict", f"{row['receipt_id']} changed while moving; nothing was moved")
    _log(con, "adopt-bind", row["receipt_id"])
    intake_notify.rebind(con, row["receipt_id"], row["owner_orch"], new_owner)


def adopt_receipts(con, old_owners, new_orch):
    """#62: received receipts follow an Orch replacement. Runs inside adopt's write transaction (never opens
    its own), so any error rolls the task move back too. acked and excluded receipts keep their owner."""
    old_owners = list(dict.fromkeys(old_owners))
    if not old_owners:
        return []
    rows = con.execute(f"SELECT * FROM intake_events WHERE state='received' AND owner_orch IN ({','.join('?' * len(old_owners))}) "
                       "ORDER BY id", old_owners).fetchall()
    for row in rows:
        _move_received(con, row, new_orch)
    return [row["receipt_id"] for row in rows]


def received_of(con, owners):
    owners = list(dict.fromkeys(owners))
    if not owners:
        return []
    return [r["receipt_id"] for r in con.execute(
        f"SELECT receipt_id FROM intake_events WHERE state='received' AND owner_orch IN ({','.join('?' * len(owners))}) "
        "ORDER BY id", owners)]


def take_unassigned(con, receipt_ids, caller):
    """An Orch takes unowned received receipts for itself (only itself; the caller id is the one identity).
    Atomic batch: anything owned by someone else or not received rejects the whole call. Own = unchanged."""
    if (not isinstance(receipt_ids, list) or not receipt_ids
            or not all(isinstance(r, str) and r for r in receipt_ids)):
        raise IntakeError("invalid", "receipt_ids must be a non-empty list of receipt ids")
    from . import intake_notify
    with store.immediate(con):
        _check_owner(con, caller)
        rows = [_event(con, r) for r in dict.fromkeys(receipt_ids)]
        for row in rows:
            if row["state"] != "received":
                raise IntakeError("invalid_state", f"{row['receipt_id']} is {row['state']}; only received events can be taken")
            if row["owner_orch"] not in (None, caller):
                raise IntakeError("not_owner", f"{row['receipt_id']} already belongs to another Orch; "
                                  "only the Operator can re-bind it (orchd intake bind)")
        results = []
        for row in rows:
            if row["owner_orch"] == caller:
                results.append(_receipt(row, "unchanged"))
                continue
            if con.execute("UPDATE intake_events SET owner_orch=? WHERE receipt_id=? AND state='received' "
                           "AND owner_orch IS NULL", (caller, row["receipt_id"])).rowcount != 1:
                raise IntakeError("not_owner", f"{row['receipt_id']} was taken by another Orch; nothing was bound")
            _log(con, "bind", row["receipt_id"])
            intake_notify.rebind(con, row["receipt_id"], None, caller)
            results.append(_receipt(_event(con, row["receipt_id"]), "bound"))
        return dict(status="bound", receipts=results)


def _stranded(con, jobs):
    """Received receipts owned by a stopped or confirmed-dead Orch (same dead rule as adopt). An unknown
    probe is not proof; unassigned receipts are never touched (no auto-assignment, docs/intake.md)."""
    from .orch_health import owner_health
    stranded = []
    for owner in [r[0] for r in con.execute("SELECT DISTINCT owner_orch FROM intake_events "
                                            "WHERE state='received' AND owner_orch IS NOT NULL ORDER BY owner_orch")]:
        orch = store.get_orch(con, owner)
        if orch is not None and (orch["stopped_at"] is not None or owner_health(orch, jobs)["state"] == "dead"):
            stranded += con.execute("SELECT * FROM intake_events WHERE state='received' AND owner_orch=? ORDER BY id",
                                    (owner,)).fetchall()
    return sorted(stranded, key=lambda r: r["id"])


def _reconcile_target(con, jobs):
    from .orch_health import owner_health
    bound = con.execute("SELECT orch_id FROM entries WHERE id='desktop'").fetchone()
    orch = store.get_orch(con, bound["orch_id"]) if bound else None
    if orch is None:
        return None, "no desktop binding to a registered Orch"
    if orch["stopped_at"] is not None:
        return orch["id"], f"bound orch {orch['id']} is not live (stopped)"
    state = owner_health(orch, jobs)
    return orch["id"], None if state["state"] == "alive" else f"bound orch {orch['id']} is not live ({state['reason']})"


def reconcile_path(path, apply=False, backup_dir=None, jobs=None):
    """`orchd intake reconcile`: move received receipts stranded on a stopped/dead Orch to the Orch the Desktop
    entry is bound to. The listing and the backup use a read-only connection (store.connect migrates the schema,
    which is a write); only an apply with something to move then opens the database normally."""
    from . import entry
    path = Path(path)
    if not path.exists():
        return dict(moved=[], backup=None, applied=False, to_orch=None)
    ro = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
    ro.row_factory = sqlite3.Row
    try:
        try:
            pending = _stranded(ro, jobs)
            target, problem = _reconcile_target(ro, jobs)
        except sqlite3.OperationalError as error:
            if "no such table" not in str(error):
                raise
            pending, target, problem = [], None, None  # a DB that predates intake has nothing stranded
        ids = [r["receipt_id"] for r in pending]
        if not apply:
            return dict(moved=ids, backup=None, applied=False, to_orch=target, target_problem=problem,
                        from_orch=sorted({r["owner_orch"] for r in pending}))
        if not pending:
            return dict(moved=[], backup=None, applied=False, to_orch=target)
        if problem:
            raise ValueError(f"cannot reconcile: {problem}; bind a live Orch first (orchd binding --to ID)")
        backup = entry._backup(ro, backup_dir, "pre-intake-reconcile")
    finally:
        ro.close()
    con = store.connect(path)
    try:
        with store.immediate(con):
            rows = _stranded(con, jobs)  # re-read under the lock
            target, problem = _reconcile_target(con, jobs)
            if problem:
                raise ValueError(f"cannot reconcile: {problem}")
            for row in rows:
                _move_received(con, row, target)
        return dict(moved=[r["receipt_id"] for r in rows], backup=str(backup), applied=True, to_orch=target)
    finally:
        con.close()


def ack(con, receipt_ids, *, actor="orch"):
    """Owner has durably taken responsibility (later than submit/bind, not business completion).
    Idempotent per receipt. An unassigned event has no owner to have taken responsibility."""
    if actor not in ACTORS:
        raise IntakeError("invalid", "actor must be orch or operator")
    with store.immediate(con):
        rows = [_event(con, r) for r in receipt_ids]
        for row in rows:
            if row["state"] == "excluded":
                raise IntakeError("invalid_state", "an excluded event cannot be acked")
            if row["owner_orch"] is None:
                raise IntakeError("invalid_state", f"{row['receipt_id']} has no owner; bind one first")
            if row["state"] != "acked" and row["version"] == 2 and con.execute(
                    "SELECT 1 FROM intake_decisions WHERE receipt_id=? AND state='completed'",
                    (row["receipt_id"],)).fetchone() is None:
                raise IntakeError("invalid_state", f"{row['receipt_id']} is a version-2 event: it is acked by a "
                                  "completed decision (orchd intake decide / resolve), not by a bare ack")
        results = []
        for row in rows:
            if row["state"] == "acked":
                results.append(_receipt(row, "duplicate"))
                continue
            con.execute("UPDATE intake_events SET state='acked',acked_at=?,acked_by=? WHERE receipt_id=?",
                        (time.time(), actor, row["receipt_id"]))
            _log(con, "ack", row["receipt_id"])
            results.append(_receipt(_event(con, row["receipt_id"]), "acked"))
        return dict(status="acked", receipts=results)


def classify(con, receipt_id, fields):
    """Orch-side annotations W4/W5 project: A/B/C/D, L0-L2, todo_id, issue_ref, project. Not a TODO store."""
    allowed = {"classification", "level", "todo_id", "issue_ref", "project"}
    if not isinstance(fields, dict) or not fields or set(fields) - allowed:
        raise IntakeError("invalid", f"fields must be a non-empty object using only {', '.join(sorted(allowed))}")
    if "classification" in fields and fields["classification"] not in CLASSIFICATIONS:
        raise IntakeError("invalid", "classification must be A, B, C or D")
    if "level" in fields and fields["level"] not in LEVELS:
        raise IntakeError("invalid", "level must be L0, L1 or L2")
    if fields.get("issue_ref") is not None and not (isinstance(fields["issue_ref"], str)
                                                     and ISSUE_REF.fullmatch(fields["issue_ref"])):
        raise IntakeError("invalid", "issue_ref must be github:owner/repo#N or jira:KEY-N")
    for name in ("todo_id", "project"):
        if fields.get(name) is not None:
            _text(fields[name], name, limit=200)
    with store.immediate(con):
        row = _event(con, receipt_id)
        if row["state"] != "received":
            raise IntakeError("invalid_state", f"an {row['state']} event cannot be classified")
        sets = ",".join(f"{k}=?" for k in fields)
        con.execute(f"UPDATE intake_events SET {sets} WHERE receipt_id=?", (*fields.values(), receipt_id))
        _log(con, "classify", receipt_id)
        return _receipt(_event(con, receipt_id), "classified")


# --- reply drafts: pending_approval -> approved -> sending -> sent | failed | uncertain; or cancelled/superseded ---

def _draft_row(con, draft_id, version):
    row = con.execute("SELECT * FROM intake_drafts WHERE draft_id=? AND version=?", (draft_id, version)).fetchone()
    if row is None:
        raise IntakeError("not_found", f"unknown draft {draft_id} v{version}")
    return row


def _latest(con, draft_id):
    return con.execute("SELECT MAX(version) FROM intake_drafts WHERE draft_id=?", (draft_id,)).fetchone()[0]


def _draft_receipt(row, status, **extra):
    return dict(status=status, draft_id=row["draft_id"], version=row["version"], state=row["state"],
                body_sha256=row["body_sha256"], expires_at=row["expires_at"], **extra)


def draft(con, payload, *, actor="orch"):
    """New draft, or the next version of an existing draft_id (older versions become superseded and their
    approvals stop counting). Any content change is a new version and needs a fresh approval."""
    if actor not in ACTORS:
        raise IntakeError("invalid", "actor must be orch or operator")
    allowed = {"draft_id", "receipt_id", "provider", "sender", "target", "source_ref", "level", "template",
               "summary", "body", "expires_at"}
    if not isinstance(payload, dict) or set(payload) - allowed:
        raise IntakeError("unknown_field", "draft must be an object using only: " + ", ".join(sorted(allowed)))
    if payload.get("provider") not in PROVIDERS:
        raise IntakeError("invalid", f"provider must be one of {', '.join(PROVIDERS)}")
    if payload.get("level", "L2") not in LEVELS:
        raise IntakeError("invalid", "level must be L0, L1 or L2")
    fields = dict(sender=_text(payload.get("sender"), "sender"), target=_text(payload.get("target"), "target"),
                  source_ref=_text(payload.get("source_ref"), "source_ref"),
                  summary=_text(payload.get("summary"), "summary", limit=SUMMARY_MAX),
                  body=_text(payload.get("body"), "body", limit=BODY_MAX))
    expires_at = _time(payload.get("expires_at"), "expires_at")
    template = _text(payload.get("template"), "template", required=False)
    draft_id = payload.get("draft_id") or "dr_" + uuid.uuid4().hex[:12]
    now = time.time()
    with store.immediate(con):
        if payload.get("receipt_id") is not None:
            event = _event(con, payload["receipt_id"])
            if event["state"] == "excluded":
                raise IntakeError("invalid_state", "an excluded event cannot be used for a draft")
        latest = _latest(con, draft_id)
        digest = _sha(fields["body"])
        if latest is not None:
            prior = _draft_row(con, draft_id, latest)
            if prior["state"] in ("sending", "sent", "uncertain"):
                raise IntakeError("invalid_state", f"draft is {prior['state']}; it cannot be revised")
            same = dict(receipt_id=payload.get("receipt_id"), provider=payload["provider"],
                        level=payload.get("level", "L2"), template=template, expires_at=expires_at,
                        body_sha256=digest, **{k: v for k, v in fields.items() if k != "body"})
            if all(prior[k] == v for k, v in same.items()) and prior["state"] in ("pending_approval", "approved"):
                return _draft_receipt(prior, "unchanged")
            con.execute("UPDATE intake_drafts SET state='superseded',updated_at=? WHERE draft_id=? AND version=? "
                        "AND state IN ('pending_approval','approved')", (now, draft_id, latest))
        version = (latest or 0) + 1
        con.execute(
            "INSERT INTO intake_drafts(draft_id,version,receipt_id,provider,sender,target,source_ref,level,template,"
            "summary,body,body_sha256,expires_at,state,created_by,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'pending_approval',?,?,?)",
            (draft_id, version, payload.get("receipt_id"), payload["provider"], fields["sender"], fields["target"],
             fields["source_ref"], payload.get("level", "L2"), template, fields["summary"], fields["body"], digest,
             expires_at, actor, now, now))
        _log(con, "draft", f"{draft_id}:{version}")
        return _draft_receipt(_draft_row(con, draft_id, version), "drafted")


def show_draft(con, draft_id, version):
    """Read-only: the full draft (including body) for one exact version, so it can be shown before approval."""
    if isinstance(version, bool) or not isinstance(version, int):
        raise IntakeError("invalid", "version must be an integer")
    row = _draft_row(con, draft_id, version)
    approval = con.execute("SELECT approval_id FROM intake_approvals WHERE draft_id=? AND version=?",
                           (draft_id, version)).fetchone()
    view = {k: row[k] for k in row.keys()}
    view.update(status="draft", is_latest=version == _latest(con, draft_id),
                expired=row["state"] in ("pending_approval", "approved") and time.time() >= row["expires_at"],
                approval_id=approval[0] if approval else None)
    return view


def approve(con, payload, *, actor="operator"):
    """Approval bound to (draft_id, version, body_sha256, sender, target, source_ref, idempotency_key).
    Stale, expired, mismatched or already-moved drafts are refused. Replaying the same key returns the
    original approval; the same key with different content is an idempotency_conflict."""
    if actor not in ACTORS:
        raise IntakeError("invalid", "actor must be orch or operator")
    need = ("draft_id", "version", "body_sha256", "sender", "target", "source_ref", "idempotency_key")
    if not isinstance(payload, dict) or set(payload) != set(need):
        raise IntakeError("invalid", f"approval must carry exactly: {', '.join(need)}")
    key = _text(payload["idempotency_key"], "idempotency_key")
    if isinstance(payload["version"], bool) or not isinstance(payload["version"], int):
        raise IntakeError("invalid", "version must be an integer")
    with store.immediate(con):
        old = con.execute("SELECT * FROM intake_approvals WHERE idempotency_key=?", (key,)).fetchone()
        if old is not None:
            if tuple(old[k] for k in need[:-1]) != tuple(payload[k] for k in need[:-1]):
                raise IntakeError("idempotency_conflict", "idempotency_key was used for a different approval")
            return dict(status="duplicate", approval_id=old["approval_id"], draft_id=old["draft_id"],
                        version=old["version"], expires_at=old["expires_at"], idempotency_key=key)
        row = _draft_row(con, payload["draft_id"], payload["version"])
        if row["state"] == "superseded" or payload["version"] != _latest(con, payload["draft_id"]) \
                or row["body_sha256"] != payload["body_sha256"]:
            raise IntakeError("stale", "draft changed since it was shown; review the latest version")
        if (row["sender"], row["target"], row["source_ref"]) != (payload["sender"], payload["target"],
                                                                payload["source_ref"]):
            raise IntakeError("target_mismatch", "sender, target or source_ref differs from the draft")
        if row["state"] != "pending_approval":
            raise IntakeError("invalid_state", f"draft is {row['state']}")
        if time.time() >= row["expires_at"]:
            raise IntakeError("expired", "draft expired; create a new version")
        approval_id = "ap_" + uuid.uuid4().hex[:12]
        con.execute("INSERT INTO intake_approvals(approval_id,draft_id,version,body_sha256,sender,target,source_ref,"
                    "expires_at,idempotency_key,actor,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (approval_id, row["draft_id"], row["version"], row["body_sha256"], row["sender"], row["target"],
                     row["source_ref"], row["expires_at"], key, actor, time.time()))
        con.execute("UPDATE intake_drafts SET state='approved',updated_at=? WHERE draft_id=? AND version=?",
                    (time.time(), row["draft_id"], row["version"]))
        _log(con, "approve", f"{row['draft_id']}:{row['version']}")
        return dict(status="approved", approval_id=approval_id, draft_id=row["draft_id"], version=row["version"],
                    expires_at=row["expires_at"], idempotency_key=key)


def claim(con, draft_id, version):
    """The connector's pre-send step: approved -> sending, once. Returns the key the connector must use
    to deduplicate its own send. A second claim never hands the draft out again."""
    with store.immediate(con):
        row = _draft_row(con, draft_id, version)
        if row["state"] in ("sending", "sent", "uncertain", "failed"):
            raise IntakeError("already_claimed", f"draft is {row['state']}; automatic resend is refused")
        if row["state"] != "approved":
            raise IntakeError("invalid_state", f"draft is {row['state']}, not approved")
        if version != _latest(con, draft_id):
            raise IntakeError("stale", "a newer version exists")
        if time.time() >= row["expires_at"]:
            raise IntakeError("expired", "approval expired; nothing sent")
        approval = con.execute("SELECT * FROM intake_approvals WHERE draft_id=? AND version=?",
                               (draft_id, version)).fetchone()
        con.execute("UPDATE intake_drafts SET state='sending',updated_at=? WHERE draft_id=? AND version=?",
                    (time.time(), draft_id, version))
        _log(con, "claim", f"{draft_id}:{version}")
        return dict(status="claimed", draft_id=draft_id, version=version, state="sending",
                    sender=row["sender"], target=row["target"], body=row["body"], body_sha256=row["body_sha256"],
                    send_idempotency_key=approval["idempotency_key"])


OUTCOMES = ("sent", "failed", "uncertain")


def record_receipt(con, payload, *, actor="connector"):
    """Connector outcome. `sent` needs the connector's own receipt id and only comes from here. `uncertain`
    stops all automatic resends; an operator may later resolve it to sent or failed."""
    if not isinstance(payload, dict) or set(payload) - {"draft_id", "version", "outcome", "connector_receipt_id",
                                                        "detail"}:
        raise IntakeError("invalid", "receipt must use only: draft_id, version, outcome, connector_receipt_id, detail")
    if payload.get("outcome") not in OUTCOMES:
        raise IntakeError("invalid", f"outcome must be one of {', '.join(OUTCOMES)}")
    if actor not in ("connector", "operator"):
        raise IntakeError("invalid", "actor must be connector or operator")
    outcome, receipt_id = payload["outcome"], payload.get("connector_receipt_id")
    if outcome == "sent":
        _text(receipt_id, "connector_receipt_id")
    detail = _text(payload.get("detail"), "detail", limit=500, required=False)
    with store.immediate(con):
        row = _draft_row(con, payload.get("draft_id"), payload.get("version"))
        if row["state"] == outcome and (row["connector_receipt_id"] or None) == (receipt_id or None):
            return _draft_receipt(row, "duplicate")
        resolving = row["state"] == "uncertain" and actor == "operator" and outcome in ("sent", "failed")
        if row["state"] != "sending" and not resolving:
            raise IntakeError("invalid_state", f"draft is {row['state']}; receipt not accepted")
        con.execute("UPDATE intake_drafts SET state=?,connector_receipt_id=?,outcome_detail=?,updated_at=? "
                    "WHERE draft_id=? AND version=?", (outcome, receipt_id, detail, time.time(),
                                                       row["draft_id"], row["version"]))
        _log(con, "receipt", f"{row['draft_id']}:{row['version']}")
        return _draft_receipt(_draft_row(con, row["draft_id"], row["version"]), "recorded")


def cancel(con, draft_id, *, actor="operator"):
    if actor not in ACTORS:
        raise IntakeError("invalid", "actor must be orch or operator")
    with store.immediate(con):
        row = _draft_row(con, draft_id, _latest(con, draft_id) or 0)
        if row["state"] == "cancelled":
            return _draft_receipt(row, "duplicate")
        if row["state"] not in ("pending_approval", "approved"):
            raise IntakeError("invalid_state", f"draft is {row['state']}; it cannot be cancelled")
        con.execute("UPDATE intake_drafts SET state='cancelled',updated_at=? WHERE draft_id=? AND version=?",
                    (time.time(), draft_id, row["version"]))
        _log(con, "cancel", f"{draft_id}:{row['version']}")
        return _draft_receipt(_draft_row(con, draft_id, row["version"]), "cancelled")


# --- read-only projection (snapshot v2 `intake`) ---

TABLES = ("intake_log", "intake_events", "intake_drafts", "intake_approvals")
ITEM_LIMIT = 500


def project(con, tables, now):
    """`intake` capability for snapshot v2. Only SELECTs. A DB without the intake tables reports
    available=false (never an empty list). Bodies are never projected; drafts show summary and hash."""
    if not all(t in tables for t in TABLES):
        return dict(version=VERSION, available=False, revision=None, auto_send=AUTO_SEND, warnings=[], sources=[],
                    items=[], drafts=[])
    from . import intake_decide
    events = con.execute("SELECT * FROM intake_events ORDER BY id DESC LIMIT ?", (ITEM_LIMIT,)).fetchall()[::-1]
    stopped = {r[0] for r in con.execute("SELECT id FROM orchs WHERE stopped_at IS NOT NULL")}
    items, warnings = [], []
    for row in events:
        item = _event_view(row)
        item.update(trust="third_party_data",
                    owner_state=("unassigned" if row["owner_orch"] is None else
                                 "owner_stopped" if row["owner_orch"] in stopped else "assigned"))
        age = now - row["received_at"]
        item["retention"] = ("excluded" if row["state"] == "excluded" else
                             "acked" if row["state"] == "acked" else
                             "held" if ("intake_decisions" in tables and intake_decide.retention_blocked(con, row["receipt_id"])) else
                             "expired" if age >= RETENTION_SECONDS else
                             "expiring" if age >= RETENTION_SECONDS - WARN_BEFORE_SECONDS else "ok")
        item["draft_refs"] = [dict(draft_id=d["draft_id"], version=d["version"]) for d in con.execute(
            "SELECT draft_id,version FROM intake_drafts WHERE receipt_id=? ORDER BY draft_id,version",
            (row["receipt_id"],))]
        items.append(item)
        if row["state"] == "received":
            for code, hit in (("unassigned", item["owner_state"] == "unassigned"),
                              ("owner_stopped", item["owner_state"] == "owner_stopped"),
                              ("retention_" + item["retention"], item["retention"] in ("expiring", "expired"))):
                if hit:
                    warnings.append(dict(code=code, receipt_id=row["receipt_id"], provider=row["provider"],
                                         account=row["account"]))
    latest = {r[0]: r[1] for r in con.execute("SELECT draft_id,MAX(version) FROM intake_drafts GROUP BY draft_id")}
    drafts = []
    for d in con.execute("SELECT * FROM intake_drafts ORDER BY created_at,draft_id,version"):
        item = {k: d[k] for k in ("draft_id", "version", "receipt_id", "provider", "sender", "target", "source_ref",
                                  "level", "template", "summary", "body_sha256", "expires_at", "state",
                                  "created_by", "created_at", "updated_at", "connector_receipt_id",
                                  "outcome_detail")}
        item["is_latest"] = d["version"] == latest[d["draft_id"]]
        item["expired"] = d["state"] in ("pending_approval", "approved") and now >= d["expires_at"]
        item["approval_id"] = (con.execute("SELECT approval_id FROM intake_approvals WHERE draft_id=? AND version=?",
                                           (d["draft_id"], d["version"])).fetchone() or [None])[0]
        drafts.append(item)
    sources = [dict(provider=r["provider"], account=r["account"], company=r["company"], received=r["n"],
                    unacked=r["unacked"], excluded=r["excluded"], last_received_at=r["last"]) for r in con.execute(
        "SELECT provider,account,company,COUNT(*) n,SUM(state='received') unacked,"
        "SUM(state='excluded') excluded,MAX(received_at) last "
        "FROM intake_events GROUP BY provider,account,company ORDER BY provider,account")]
    return dict(version=VERSION, available=True, revision=revision(con), auto_send=AUTO_SEND, warnings=warnings,
                sources=sources, items=items, drafts=drafts)
