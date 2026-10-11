"""Dashboard contracts v1 (default) and opt-in v2. Only SELECTs on a private copy; no migrations or runtime calls.

Entry questions are distinct from unread worker questions (awaiting Orch triage).
Worker answer evidence links only through persisted entry_reply_id; missing links remain null.
Timestamps and persisted column names retain the store schema's meaning.
schema_version versions this output contract; schema_fingerprint identifies the
actual database schema. An incomplete schema fails explicitly, without migration.
answered_limit bounds answered question history only; worker evidence is unbounded.
"""
import hashlib
import json
import time
from pathlib import Path

from . import entry, goals, stats, store, task_questions


def _metadata(row):
    # Bodies are intentionally excluded except the entry/worker question detail.
    return {key: value for key, value in dict(row).items() if key not in ("body", "wire_text", "source_raw")}


def read(path, *, answered_limit=100, schema_version=1):
    if schema_version not in (1, 2):
        raise ValueError("--schema-version must be 1 or 2")
    if answered_limit < 0:
        raise ValueError("--answered-limit must be non-negative")
    path = Path(path).expanduser().resolve()
    with stats.open_snapshot(path) as snap:
        con = snap.con
        con.execute("PRAGMA query_only=ON")
        generated_at = time.time()
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        schema = [tuple(r) for r in con.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name")]
        task_rows = {r["id"]: dict(r) for r in con.execute(
            "SELECT id,title,status,repo,goal_id,orch_thread,created_at,updated_at FROM tasks ORDER BY created_at,id")}
        tasks = [dict(t, latest_progress=None, latest_report=None) for t in task_rows.values()
                 if t["status"] in store.OPEN]
        recent = {}
        for row in con.execute("SELECT * FROM messages WHERE kind IN ('progress','report') ORDER BY created_at,id"):
            recent[row["task_id"], row["kind"]] = _metadata(row)
        for task in tasks:
            for kind in ("progress", "report"):
                task["latest_" + kind] = recent.get((task["id"], kind))

        bindings = {r["id"]: r["orch_id"] for r in con.execute("SELECT id,orch_id FROM entries")}
        states = ("current", "queued") if schema_version == 1 else ("current", "queued", "snoozed", "cancelled", "superseded")
        questions = [dict(r) for r in con.execute(
            f"SELECT * FROM entry_messages WHERE kind='question' AND question_state IN ({','.join('?' * len(states))}) ORDER BY id", states)]
        questions += [dict(r) for r in con.execute(
            "SELECT * FROM entry_messages WHERE kind='question' AND question_state='answered' ORDER BY id DESC LIMIT ?",
            (answered_limit,))]
        entry_messages = []
        for question in questions:
            task = task_rows.get(question["task_id"])
            item = _metadata(question)
            if schema_version == 2:
                item.update({key: question.get(key, 1 if key == "version" else None) for key in
                             ("version", "supersedes", "superseded_by", "revisit_at", "note", "reason")})
                item["revisit_due"] = (question["question_state"] == "snoozed" and item["revisit_at"] is not None
                                        and item["revisit_at"] <= generated_at)
                item["history"] = [dict(r) for r in con.execute(
                    "SELECT * FROM entry_question_history WHERE question_id=? ORDER BY id DESC LIMIT 10",
                    (question["id"],))][::-1] if "entry_question_history" in tables else []
            item.update(body=question["body"],
                        binding_current=(bindings[question["entry_id"]] == question["orch_id"]
                                         if question["entry_id"] in bindings else None),
                        task_open=(task["status"] in store.OPEN if task else None),
                        task=task, audience="nat")
            entry_messages.append(item)

        worker_answers = [_metadata(r) for r in con.execute(
            "SELECT * FROM messages WHERE kind IN ('answer_queued','answer') ORDER BY id")]
        linked = {}
        for answer in worker_answers:
            if schema_version == 2:
                answer.setdefault("entry_reply_id", None)
            answer["processing"] = ("pending" if answer["kind"] == "answer_queued" and answer["read_at"] is None
                                    else "transport_accepted")
            answer["reply_id"] = answer.get("entry_reply_id") if schema_version == 2 else None
            if answer["reply_id"] is not None:
                linked.setdefault(answer["reply_id"], []).append(answer)
        question_ids = {q["id"] for q in questions}
        replies = []
        for row in con.execute("SELECT * FROM entry_messages WHERE kind='reply' ORDER BY id"):
            if row["reply_to"] not in question_ids:
                continue
            item = _metadata(row)
            evidence = linked.get(row["id"], [])
            forwarded = ("transport_accepted" if any(a["processing"] == "transport_accepted" for a in evidence)
                         else "pending" if evidence else None)
            if schema_version == 1:
                item["processing"] = dict(
                    saved=True,
                    orch_notification={"pending": "pending", "failed": "failed", "sending": "uncertain",
                                       "delivered": "notified"}.get(row["delivery"], "unknown"),
                    orch_read_at=row["read_at"],
                    worker_forwarded="unknown", worker_confirmed="unknown", worker_executed="unknown")
            else:
                item["processing"] = dict(
                    saved=True, saved_at=row["created_at"],
                    orch_notification={"pending": "pending", "failed": "failed", "sending": "uncertain",
                                       "delivered": "notified" if item.get("notified_at") is not None else None}.get(row["delivery"], None),
                    orch_notified_at=item.get("notified_at"),
                    orch_read_at=row["read_at"],
                    worker_forwarded=forwarded, worker_answer_ids=[a["id"] for a in evidence],
                    worker_confirmed="unknown", worker_executed="unknown")
            replies.append(item)

        worker_questions = []
        for row in con.execute("SELECT * FROM messages WHERE kind='question' AND read_at IS NULL ORDER BY id"):
            item = _metadata(row)
            item.update(body=row["body"], task=task_rows.get(row["task_id"]),
                        audience="orch", processing="awaiting_orch_triage")
            worker_questions.append(item)
        # App receipts are separate evidence, never joined to a reply by body or task status.
        deliveries = [dict(r) for r in con.execute("SELECT * FROM worker_deliveries ORDER BY created_at,id")]
        goal_rows = [goals.get(con, r["id"], history=True) for r in con.execute(
            "SELECT id FROM goals ORDER BY repo,created_at,id")]
        return dict(generated_at=generated_at, source_db=str(path), schema_version=schema_version,
                    schema_fingerprint=hashlib.sha256(json.dumps(schema).encode()).hexdigest(),
                    answered_limit=answered_limit, goals=goal_rows, tasks=tasks,
                    entry_messages=entry_messages, replies=replies, worker_questions=worker_questions,
                    worker_answers=worker_answers, worker_deliveries=deliveries,
                    **(dict(pending_worker_questions=task_questions.pending(con, task_rows.values()),
                            binding_counts=[dict(entry_id=eid, orch_id=oid, **entry.message_counts(con, eid, oid))
                                            for eid, oid in sorted(bindings.items())])
                       if schema_version == 2 else {}))
