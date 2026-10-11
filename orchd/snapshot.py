"""Dashboard contract v1. Only SELECTs on a private copy; no migrations or runtime calls.

Entry questions are distinct from unread worker questions (awaiting Orch triage).
Worker answer evidence is task-scoped: there is no exact reply-to-worker bridge.
Timestamps and persisted column names retain the store schema's meaning.
schema_version versions this output contract; schema_fingerprint identifies the
actual database schema. An incomplete schema fails explicitly, without migration.
answered_limit bounds answered question history only; worker evidence is unbounded.
"""
import hashlib
import json
import time
from pathlib import Path

from . import goals, stats, store


def _metadata(row):
    # Bodies are intentionally excluded except the entry/worker question detail.
    return {key: value for key, value in dict(row).items() if key not in ("body", "wire_text", "source_raw")}


def read(path, *, answered_limit=100):
    if answered_limit < 0:
        raise ValueError("--answered-limit must be non-negative")
    path = Path(path).expanduser().resolve()
    with stats.open_snapshot(path) as snap:
        con = snap.con
        con.execute("PRAGMA query_only=ON")
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
        questions = [dict(r) for r in con.execute(
            "SELECT * FROM entry_messages WHERE kind='question' AND question_state IN ('current','queued') ORDER BY id")]
        questions += [dict(r) for r in con.execute(
            "SELECT * FROM entry_messages WHERE kind='question' AND question_state='answered' ORDER BY id DESC LIMIT ?",
            (answered_limit,))]
        entry_messages = []
        for question in questions:
            task = task_rows.get(question["task_id"])
            item = _metadata(question)
            item.update(body=question["body"],
                        binding_current=(bindings[question["entry_id"]] == question["orch_id"]
                                         if question["entry_id"] in bindings else None),
                        task_open=(task["status"] in store.OPEN if task else None),
                        task=task, audience="nat")
            entry_messages.append(item)

        question_ids = {q["id"] for q in questions}
        replies = []
        for row in con.execute("SELECT * FROM entry_messages WHERE kind='reply' ORDER BY id"):
            if row["reply_to"] not in question_ids:
                continue
            item = _metadata(row)
            item["processing"] = dict(
                saved=True,
                orch_notification={"pending": "pending", "failed": "failed", "sending": "uncertain",
                                   "delivered": "notified"}.get(row["delivery"], "unknown"),
                orch_read_at=row["read_at"],
                worker_forwarded="unknown", worker_confirmed="unknown", worker_executed="unknown")
            replies.append(item)

        worker_questions = []
        for row in con.execute("SELECT * FROM messages WHERE kind='question' AND read_at IS NULL ORDER BY id"):
            item = _metadata(row)
            item.update(body=row["body"], task=task_rows.get(row["task_id"]),
                        audience="orch", processing="awaiting_orch_triage")
            worker_questions.append(item)
        worker_answers = [_metadata(r) for r in con.execute(
            "SELECT * FROM messages WHERE kind IN ('answer_queued','answer') ORDER BY id")]
        for answer in worker_answers:
            answer["processing"] = ("pending" if answer["kind"] == "answer_queued" and answer["read_at"] is None
                                    else "transport_accepted")
            answer["reply_id"] = None  # task-level evidence cannot identify an individual entry reply
        # App receipts are separate evidence, never joined to a reply by body or task status.
        deliveries = [dict(r) for r in con.execute("SELECT * FROM worker_deliveries ORDER BY created_at,id")]
        goal_rows = [goals.get(con, r["id"], history=True) for r in con.execute(
            "SELECT id FROM goals ORDER BY repo,created_at,id")]
        return dict(generated_at=time.time(), source_db=str(path), schema_version=1,
                    schema_fingerprint=hashlib.sha256(json.dumps(schema).encode()).hexdigest(),
                    answered_limit=answered_limit, goals=goal_rows, tasks=tasks,
                    entry_messages=entry_messages, replies=replies, worker_questions=worker_questions,
                    worker_answers=worker_answers, worker_deliveries=deliveries)
