"""Task parent references in versioned messages. No tracker calls or schema changes."""
import json
import re
import time
import uuid

from . import store

KIND = "task_issue_link"
VERSION = 1
REF = re.compile(r"(?:github:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?/[A-Za-z0-9_.-]+#[1-9][0-9]*|jira:[A-Z][A-Z0-9_]*-[1-9][0-9]*)\Z")


def validate_ref(ref):
    if not isinstance(ref, str) or not REF.fullmatch(ref):
        raise ValueError("issue ref must be github:owner/repo#N or jira:KEY-N (positive N)")
    return ref


def history(con, task_id=None):
    """Chronological public audit records, including unlinks and superseded refs."""
    where = "kind=?" + (" AND task_id=?" if task_id is not None else "")
    params = (KIND, task_id) if task_id is not None else (KIND,)
    records = []
    for row in con.execute(f"SELECT id,task_id,body FROM messages WHERE {where} ORDER BY id", params):
        try:
            event = json.loads(row["body"])
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get("version") != VERSION:
            continue
        records.append(dict(message_id=row["id"], task_id=row["task_id"], **event))
    return records


def set_ref(con, task_id, ref, *, actor="orch", idempotency_key=None):
    """Serialize changes and durable key replay in one short SQLite transaction.

    Keys are task-scoped across link and unlink. Omitted keys are generated and
    returned so callers can replay them. A retry returns the original receipt,
    even if another change has since superseded it; it never restores an old ref.
    """
    if ref is not None:
        validate_ref(ref)
    if actor not in ("orch", "operator"):
        raise ValueError("actor must be orch or operator")
    if idempotency_key is not None and (not isinstance(idempotency_key, str) or not idempotency_key.strip()):
        raise ValueError("idempotency key must be non-empty")
    key = idempotency_key if idempotency_key is not None else uuid.uuid4().hex
    with store.immediate(con):
        store.get_task(con, task_id)
        events = history(con, task_id)
        for event in events:
            if event["idempotency_key"] == key:
                if (event["ref"], event["actor"]) != (ref, actor):
                    raise ValueError("idempotency conflict: key was used with a different ref or actor")
                return event["receipt"]
        previous = events[-1] if events else None
        before = previous["ref"] if previous else None
        created_at = time.time()
        receipt = dict(status="linked" if ref is not None else "unlinked", task_id=task_id,
                       issue_ref=ref, changed=before != ref, actor=actor, created_at=created_at,
                       supersedes=previous["message_id"] if previous else None, idempotency_key=key)
        event = dict(version=VERSION, ref=ref, actor=actor, created_at=created_at,
                     supersedes=receipt["supersedes"], idempotency_key=key, receipt=receipt)
        message_id = store.add_message(con, task_id, KIND, json.dumps(event))
        receipt["message_id"] = message_id
        con.execute("UPDATE messages SET body=? WHERE id=?", (json.dumps(event), message_id))
        return receipt


def link(con, task_id, ref, **options):
    validate_ref(ref)  # null is reserved for the explicit unlink action
    return set_ref(con, task_id, ref, **options)


def unlink(con, task_id, **options):
    return set_ref(con, task_id, None, **options)
