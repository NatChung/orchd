"""Local intake contract: durable queue for polled source events, plus reply drafts, approvals and send receipts.

Producers (source adapters) only call the `orchd intake` CLI; nothing here reads or writes sources. Third-party
content is stored as short metadata (`summary`), marked data, and never becomes an Entry message, a task or a
permission. Every state change appends one `intake_log` row; its max seq is the snapshot `revision`.
Contract reference: docs/intake.md. Frozen fixtures: tests/fixtures/intake/.
"""
import hashlib
import json
import re
import time
import uuid

from . import store

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
REQUIRED = ("version", "provider", "account", "company", "event_id", "source_ref", "source_time", "summary")
ISSUE_REF = re.compile(r"(?:github:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?/[A-Za-z0-9_.-]+#[1-9][0-9]*"
                       r"|jira:[A-Z][A-Z0-9_]*-[1-9][0-9]*)\Z")

ERROR_CODES = ("invalid", "unknown_field", "producer_not_allowed", "account_not_allowed", "unknown_owner",
               "conflict", "not_found", "stale", "expired", "target_mismatch", "invalid_state",
               "already_claimed", "idempotency_conflict")


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
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise IntakeError("invalid", f"{name} must be a positive unix timestamp")
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
    return {k: row[k] for k in keys}


def _receipt(row, status):
    return dict(status=status, receipt_id=row["receipt_id"], provider=row["provider"], account=row["account"],
                event_id=row["event_id"], received_at=row["received_at"], state=row["state"],
                owner_orch=row["owner_orch"],
                owner_state="assigned" if row["owner_orch"] else "unassigned",
                content_sha256=row["content_sha256"])


def submit(con, payload, *, producer, producers):
    """Durable receipt for one normalized event. Same key and content returns the original receipt as
    `duplicate`; same key with different content is `conflict` and changes nothing. Validation and the
    producer grant run before any write."""
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


def pending(con, owner=None, limit=100):
    """Unacked events for one owner (or unassigned when owner is None). Read-only: nothing is consumed."""
    if owner is None:
        rows = con.execute("SELECT * FROM intake_events WHERE state='received' AND owner_orch IS NULL "
                           "ORDER BY id LIMIT ?", (limit,))
    else:
        rows = con.execute("SELECT * FROM intake_events WHERE state='received' AND owner_orch=? "
                           "ORDER BY id LIMIT ?", (owner, limit))
    return dict(owner=owner, events=[_event_view(r) for r in rows])


def bind_owner(con, receipt_id, owner):
    with store.immediate(con):
        row = _event(con, receipt_id)
        _check_owner(con, owner)
        if row["owner_orch"] == owner:
            return _receipt(row, "unchanged")
        if row["owner_orch"] is not None and row["state"] != "received":
            raise IntakeError("invalid_state", "an acked event keeps its owner")
        con.execute("UPDATE intake_events SET owner_orch=? WHERE receipt_id=?", (owner, receipt_id))
        _log(con, "bind", receipt_id)
        return _receipt(_event(con, receipt_id), "bound")


def ack(con, receipt_ids, *, actor="orch"):
    """Processing ack by the Orch (a later stage than the durable receipt). Idempotent per receipt.
    An unassigned event cannot be acked: it has no owner to have processed it."""
    if actor not in ACTORS:
        raise IntakeError("invalid", "actor must be orch or operator")
    with store.immediate(con):
        rows = [_event(con, r) for r in receipt_ids]
        for row in rows:
            if row["owner_orch"] is None:
                raise IntakeError("invalid_state", f"{row['receipt_id']} has no owner; bind one first")
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
        _event(con, receipt_id)
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
            _event(con, payload["receipt_id"])
        latest = _latest(con, draft_id)
        digest = _sha(fields["body"])
        if latest is not None:
            prior = _draft_row(con, draft_id, latest)
            if prior["state"] in ("sending", "sent", "uncertain"):
                raise IntakeError("invalid_state", f"draft is {prior['state']}; it cannot be revised")
            if (prior["body_sha256"], prior["sender"], prior["target"], prior["source_ref"], prior["expires_at"]) == \
                    (digest, fields["sender"], fields["target"], fields["source_ref"], expires_at) \
                    and prior["state"] in ("pending_approval", "approved"):
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
    events = con.execute("SELECT * FROM intake_events ORDER BY id DESC LIMIT ?", (ITEM_LIMIT,)).fetchall()[::-1]
    stopped = {r[0] for r in con.execute("SELECT id FROM orchs WHERE stopped_at IS NOT NULL")}
    items, warnings = [], []
    for row in events:
        item = _event_view(row)
        item.update(trust="third_party_data",
                    owner_state=("unassigned" if row["owner_orch"] is None else
                                 "owner_stopped" if row["owner_orch"] in stopped else "assigned"))
        age = now - row["received_at"]
        item["retention"] = ("acked" if row["state"] == "acked" else
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
                    unacked=r["unacked"], last_received_at=r["last"]) for r in con.execute(
        "SELECT provider,account,company,COUNT(*) n,SUM(state='received') unacked,MAX(received_at) last "
        "FROM intake_events GROUP BY provider,account,company ORDER BY provider,account")]
    return dict(version=VERSION, available=True, revision=revision(con), auto_send=AUTO_SEND, warnings=warnings,
                sources=sources, items=items, drafts=drafts)
