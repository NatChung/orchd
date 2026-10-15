"""Intake payload version 2: private evidence plus versioned suggestions, handed to an Orch to decide.

A producer submits the event identity, the original text (or a marked-unavailable stand-in) and optional
suggestions (project, route, urgency, candidate issues, speaker, conversation). The identity hash covers the
identity fields and the evidence digest only, so a later suggestion never makes a replay conflict: it appends a
new revision. Nothing here decides anything; decisions live in intake_decide. Third-party text is data: it is
stored and shown to the owning Orch marked as such and is never parsed for commands or approvals.
Contract reference: docs/intake.md ("Contract v3").
"""
import json
import re
import time
import uuid

from . import intake, store

VERSION = 2
CONTENT_MAX = 100_000
ATTACHMENT_MAX = 20
SUGGESTION_MAX_BYTES = 20_000
CONTENT_STATUSES = ("complete", "truncated", "unavailable")
ATTACHMENT_STATUSES = ("referenced", "unavailable")
URGENCIES = ("urgent", "normal")
EVENT_FIELDS = intake.EVENT_FIELDS | {"content", "suggestion"}
CONTENT_FIELDS = {"text", "status", "unavailable_reason", "attachments"}
ATTACHMENT_FIELDS = {"name", "sha256", "bytes", "status", "note"}
SUGGESTION_FIELDS = {"routing", "triage", "related_issues", "candidates_status", "speaker", "conversation"}
ROUTING_FIELDS = {"project", "repo", "mode", "rule_version", "evidence"}
TRIAGE_FIELDS = {"classification", "urgency", "reason"}
ISSUE_FIELDS = {"issue_ref", "state", "reason"}
SPEAKER_FIELDS = {"person_id", "name", "company", "role", "confirmation", "roster_version", "forwarded_by"}
CONVERSATION_FIELDS = {"platform", "kind", "ref", "name"}
CANDIDATE_STATUSES = ("complete", "candidates_unknown", "none")
CONFIRMATIONS = ("confirmed", "unconfirmed", "unknown")
CONVERSATION_KINDS = ("group", "channel", "dm", "email")
ISSUE_STATES = ("open", "closed", "unknown")
SHA = re.compile(r"[0-9a-f]{64}\Z")


def _object(value, name, allowed, *, required=()):
    if not isinstance(value, dict):
        raise intake.IntakeError("invalid", f"{name} must be an object")
    extra = sorted(set(value) - allowed)
    if extra:
        raise intake.IntakeError("unknown_field", f"unknown {name} field(s): {', '.join(extra)}")
    missing = [k for k in required if value.get(k) is None]
    if missing:
        raise intake.IntakeError("invalid", f"{name} missing field(s): {', '.join(missing)}")
    return value


def _choice(value, name, valid):
    if value not in valid:
        raise intake.IntakeError("invalid", f"{name} must be one of {', '.join(valid)}")
    return value


def _opt(obj, key, name, limit=200):
    return intake._text(obj.get(key), f"{name}.{key}", limit=limit, required=False)


def normalize_suggestion(value):
    """Validated, canonical suggestion dict (only the keys the producer supplied) or None for no suggestion."""
    if value is None:
        return None
    value = _object(value, "suggestion", SUGGESTION_FIELDS)
    out = {}
    if "routing" in value:
        r = _object(value["routing"], "routing", ROUTING_FIELDS)
        out["routing"] = {k: _opt(r, k, "routing", 500 if k == "evidence" else 200) for k in ROUTING_FIELDS if k in r}
    if "triage" in value:
        t = _object(value["triage"], "triage", TRIAGE_FIELDS)
        out["triage"] = {}
        if "classification" in t:
            out["triage"]["classification"] = _opt(t, "classification", "triage")
        if "urgency" in t:
            out["triage"]["urgency"] = _choice(t["urgency"], "triage.urgency", URGENCIES)
        if "reason" in t:
            out["triage"]["reason"] = _opt(t, "reason", "triage", 500)
    if "related_issues" in value:
        issues = value["related_issues"]
        if not isinstance(issues, list) or len(issues) > 20:
            raise intake.IntakeError("invalid", "related_issues must be a list of at most 20")
        out["related_issues"] = []
        for item in issues:
            item = _object(item, "related_issues item", ISSUE_FIELDS, required=("issue_ref",))
            if not (isinstance(item["issue_ref"], str) and intake.ISSUE_REF.fullmatch(item["issue_ref"])):
                raise intake.IntakeError("invalid", "related issue_ref must be github:owner/repo#N or jira:KEY-N")
            clean = {"issue_ref": item["issue_ref"]}
            if "state" in item:
                clean["state"] = _choice(item["state"], "related_issues.state", ISSUE_STATES)
            if "reason" in item:
                clean["reason"] = _opt(item, "reason", "related_issues", 300)
            out["related_issues"].append(clean)
    if "candidates_status" in value:
        out["candidates_status"] = _choice(value["candidates_status"], "candidates_status", CANDIDATE_STATUSES)
    if "speaker" in value:
        s = _object(value["speaker"], "speaker", SPEAKER_FIELDS, required=("confirmation",))
        out["speaker"] = {k: _opt(s, k, "speaker") for k in SPEAKER_FIELDS - {"confirmation"} if k in s}
        out["speaker"]["confirmation"] = _choice(s["confirmation"], "speaker.confirmation", CONFIRMATIONS)
    if "conversation" in value:
        c = _object(value["conversation"], "conversation", CONVERSATION_FIELDS, required=("kind",))
        out["conversation"] = {k: _opt(c, k, "conversation") for k in CONVERSATION_FIELDS - {"kind"} if k in c}
        out["conversation"]["kind"] = _choice(c["kind"], "conversation.kind", CONVERSATION_KINDS)
    if len(json.dumps(out, ensure_ascii=False).encode()) > SUGGESTION_MAX_BYTES:
        raise intake.IntakeError("invalid", f"suggestion exceeds {SUGGESTION_MAX_BYTES} bytes")
    return out


def _suggestion_urgency(suggestion):
    return (suggestion or {}).get("triage", {}).get("urgency", "normal")


def _normalize_content(value):
    value = _object(value, "content", CONTENT_FIELDS, required=("status",))
    status = _choice(value["status"], "content.status", CONTENT_STATUSES)
    text = value.get("text")
    if status == "unavailable":
        if text is not None:
            raise intake.IntakeError("invalid", "unavailable content must not carry text; give unavailable_reason")
        intake._text(value.get("unavailable_reason"), "content.unavailable_reason", limit=300)
    else:
        intake._text(text, "content.text", limit=CONTENT_MAX)
    attachments = value.get("attachments", [])
    if not isinstance(attachments, list) or len(attachments) > ATTACHMENT_MAX:
        raise intake.IntakeError("invalid", f"attachments must be a list of at most {ATTACHMENT_MAX}")
    clean = []
    for item in attachments:
        item = _object(item, "attachment", ATTACHMENT_FIELDS, required=("name", "status"))
        entry = dict(name=intake._text(item["name"], "attachment.name", limit=200),
                     status=_choice(item["status"], "attachment.status", ATTACHMENT_STATUSES))
        if item.get("sha256") is not None:
            if not (isinstance(item["sha256"], str) and SHA.fullmatch(item["sha256"])):
                raise intake.IntakeError("invalid", "attachment.sha256 must be 64 lowercase hex characters")
            entry["sha256"] = item["sha256"]
        if item.get("bytes") is not None:
            if isinstance(item["bytes"], bool) or not isinstance(item["bytes"], int) or item["bytes"] < 0:
                raise intake.IntakeError("invalid", "attachment.bytes must be a non-negative integer")
            entry["bytes"] = item["bytes"]
        if item.get("note") is not None:
            entry["note"] = intake._text(item["note"], "attachment.note", limit=200)
        clean.append(entry)
    return dict(status=status, text=text, reason=value.get("unavailable_reason"), attachments=clean)


def _normalize(payload):
    if not isinstance(payload, dict):
        raise intake.IntakeError("invalid", "event must be a JSON object")
    extra = sorted(set(payload) - EVENT_FIELDS)
    if extra:
        raise intake.IntakeError("unknown_field", f"unknown event field(s): {', '.join(extra)}")
    missing = [k for k in (*intake.REQUIRED, "content") if payload.get(k) is None]
    if missing:
        raise intake.IntakeError("invalid", f"missing field(s): {', '.join(missing)}")
    if payload["version"] != VERSION or isinstance(payload["version"], bool):
        raise intake.IntakeError("invalid", f"version must be {VERSION}")
    if payload["provider"] not in intake.PROVIDERS:
        raise intake.IntakeError("invalid", f"provider must be one of {', '.join(intake.PROVIDERS)}")
    event = dict(version=VERSION, provider=payload["provider"],
                 account=intake._text(payload["account"], "account"),
                 company=intake._text(payload.get("company", None) or payload["account"], "company"),
                 event_id=intake._text(payload["event_id"], "event_id"),
                 source_ref=intake._text(payload["source_ref"], "source_ref"),
                 thread_ref=intake._text(payload.get("thread_ref"), "thread_ref", required=False),
                 source_time=intake._time(payload["source_time"], "source_time"),
                 summary=intake._text(payload["summary"], "summary", limit=intake.SUMMARY_MAX))
    content = _normalize_content(payload["content"])
    owner = payload.get("owner_orch")
    suggestion = normalize_suggestion(payload.get("suggestion"))
    return event, content, suggestion, (intake._text(owner, "owner_orch") if owner is not None else None)


def _identity_hash(event, content):
    # Suggestions are excluded on purpose: a better route later must not conflict with the original event.
    identity = dict(event, content_status=content["status"],
                    content_sha256=intake._sha(content["text"]) if content["text"] is not None else None,
                    attachments=content["attachments"])
    return intake._sha(json.dumps(identity, sort_keys=True, ensure_ascii=False, separators=(",", ":")))


def _add_suggestion(con, receipt_id, suggestion, producer):
    """Append a revision unless it equals the latest one. Returns (revision, appended)."""
    digest = intake._sha(json.dumps(suggestion, sort_keys=True, ensure_ascii=False, separators=(",", ":")))
    latest = con.execute("SELECT revision,suggestion_sha256 FROM intake_suggestions WHERE receipt_id=? "
                         "ORDER BY revision DESC LIMIT 1", (receipt_id,)).fetchone()
    if latest is not None and latest["suggestion_sha256"] == digest:
        return latest["revision"], False
    revision = (latest["revision"] if latest else 0) + 1
    con.execute("INSERT INTO intake_suggestions(receipt_id,revision,suggestion_sha256,urgency,body,producer,"
                "created_at) VALUES(?,?,?,?,?,?,?)",
                (receipt_id, revision, digest, _suggestion_urgency(suggestion),
                 json.dumps(suggestion, ensure_ascii=False), producer, time.time()))
    intake._log(con, "suggest", receipt_id)
    return revision, True


def latest_revision(con, receipt_id):
    return con.execute("SELECT COALESCE(MAX(revision),0) FROM intake_suggestions WHERE receipt_id=?",
                       (receipt_id,)).fetchone()[0]


def submit(con, payload, *, producer, producers):
    """Version-2 submit. Same identity returns the original receipt as `duplicate` (a new suggestion is appended
    as a new revision); the same key with different text or attachments is `conflict` and changes nothing."""
    from . import intake_notify
    event, content, suggestion, owner = _normalize(payload)
    intake.check_producer(producers, producer, event["provider"], event["account"])
    digest = _identity_hash(event, content)
    urgency = _suggestion_urgency(suggestion)
    with store.immediate(con):
        row = con.execute("SELECT * FROM intake_events WHERE provider=? AND account=? AND event_id=?",
                          (event["provider"], event["account"], event["event_id"])).fetchone()
        if row is not None:
            if row["content_sha256"] != digest:
                raise intake.IntakeError("conflict", "event_id already received with different content; not overwritten")
            revision = latest_revision(con, row["receipt_id"])
            if suggestion is not None and row["state"] == "received":
                revision, appended = _add_suggestion(con, row["receipt_id"], suggestion, producer)
                if appended:
                    intake_notify.upgrade(con, row["receipt_id"], urgency)
            return dict(intake._receipt(row, "duplicate"), suggestion_revision=revision)
        if owner is not None:
            intake._check_owner(con, owner)
        receipt_id = "ir_" + uuid.uuid4().hex[:16]
        now = time.time()
        con.execute(
            "INSERT INTO intake_events(receipt_id,version,provider,account,company,event_id,source_ref,thread_ref,"
            "source_time,received_at,producer,summary,content_sha256,owner_orch) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (receipt_id, VERSION, event["provider"], event["account"], event["company"], event["event_id"],
             event["source_ref"], event["thread_ref"], event["source_time"], now, producer,
             event["summary"], digest, owner))
        text = content["text"]
        con.execute("INSERT INTO intake_content(receipt_id,status,text,text_sha256,text_bytes,attachments,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (receipt_id, content["status"], text, intake._sha(text) if text is not None else None,
                     len(text.encode()) if text is not None else 0,
                     json.dumps(content["attachments"], ensure_ascii=False), now))
        intake._log(con, "event", receipt_id)
        revision = 0
        if suggestion is not None:
            revision, _ = _add_suggestion(con, receipt_id, suggestion, producer)
        if owner is not None:
            intake_notify.enqueue(con, receipt_id, owner, urgency)
        receipt = intake._receipt(con.execute("SELECT * FROM intake_events WHERE receipt_id=?",
                                              (receipt_id,)).fetchone(), "received")
    return dict(receipt, suggestion_revision=revision, urgency=urgency)


def suggest(con, receipt_id, suggestion, *, producer, producers):
    """Producer: append a suggestion revision to a received v2 event (never rewrites an older one)."""
    from . import intake_notify
    suggestion = normalize_suggestion(suggestion)
    if not suggestion:
        raise intake.IntakeError("invalid", "suggestion must be a non-empty object")
    with store.immediate(con):
        row = intake._event(con, receipt_id)
        intake.check_producer(producers, producer, row["provider"], row["account"])
        if row["version"] != VERSION:
            raise intake.IntakeError("invalid_state", "suggestions need a version-2 event")
        if row["state"] != "received":
            raise intake.IntakeError("invalid_state", f"an {row['state']} event cannot take a suggestion")
        revision, appended = _add_suggestion(con, receipt_id, suggestion, producer)
        if appended:
            intake_notify.upgrade(con, receipt_id, _suggestion_urgency(suggestion))
    return dict(status="suggested" if appended else "unchanged", receipt_id=receipt_id, suggestion_revision=revision,
                urgency=_suggestion_urgency(suggestion))


def suggestions(con, receipt_id):
    return [dict(revision=r["revision"], producer=r["producer"], created_at=r["created_at"],
                 urgency=r["urgency"], suggestion=json.loads(r["body"]))
            for r in con.execute("SELECT * FROM intake_suggestions WHERE receipt_id=? ORDER BY revision",
                                 (receipt_id,))]


def show(con, receipt_id, viewer):
    """Read-only, never consumes. Original text and speaker context are readable only by the owning Orch
    (viewer = its id) or the Operator; an unassigned event is Operator-only. The result marks all of it
    third-party data: nothing in it is a command, an approval or Operator authority."""
    row = intake._event(con, receipt_id)
    if viewer != "operator" and (viewer is None or row["owner_orch"] != viewer):
        raise intake.IntakeError("not_owner", "only the owning Orch or the Operator can read an event's content")
    content = con.execute("SELECT * FROM intake_content WHERE receipt_id=?", (receipt_id,)).fetchone()
    latest = suggestions(con, receipt_id)
    return dict(status="event", trust="third_party_data", event=intake._event_view(row),
                content=None if content is None else dict(
                    status=content["status"], text=content["text"], text_sha256=content["text_sha256"],
                    bytes=content["text_bytes"], attachments=json.loads(content["attachments"])),
                suggestion=latest[-1]["suggestion"] if latest else None,
                suggestion_revision=latest[-1]["revision"] if latest else 0,
                suggestion_history=[dict(revision=s["revision"], producer=s["producer"], urgency=s["urgency"],
                                         created_at=s["created_at"]) for s in latest],
                revision=receipt_revision(con, receipt_id))


def receipt_revision(con, receipt_id):
    """Per-receipt revision for compare-and-set: the newest intake_log row that touched this receipt."""
    return con.execute("SELECT COALESCE(MAX(seq),0) FROM intake_log WHERE ref=?", (receipt_id,)).fetchone()[0]
