"""Durable dashboard actions stored in existing messages; no schema changes."""
import hashlib
import json
import time

from . import core, store, task_questions

KINDS = ("task_question_answer", "question_dismissed")


def _events(con, task_id):
    for row in con.execute("SELECT * FROM messages WHERE task_id=? AND kind IN (?,?) ORDER BY id DESC",
                           (task_id, *KINDS)):
        try:
            event = json.loads(row["body"])
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("idempotency_key") and isinstance(event.get("receipt"), dict):
            yield row, event


def latest(con, task_id):
    for row, event in _events(con, task_id):
        if row["kind"] == "task_question_answer":
            return dict(question_id=event["question_id"], answer_message_id=event["receipt"]["answer_message_id"],
                        actor=event["actor"], created_at=row["created_at"], receipt=event["receipt"])
    return None


def _validate(question_id, text, key, actor):
    if not isinstance(question_id, int) or isinstance(question_id, bool) or question_id < 1:
        raise ValueError("question id must be a positive integer")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("answer text or dismiss reason must be non-empty")
    if not isinstance(key, str) or not key.strip():
        raise ValueError("idempotency key must be non-empty")
    if actor not in ("dashboard", "operator"):
        raise ValueError("actor must be dashboard or operator")


def _checked(con, task_id, question_id, key, digest, actor, kind):
    task = store.get_task(con, task_id)
    for row, event in _events(con, task_id):
        if event["idempotency_key"] != key:
            continue
        if (row["kind"], event["question_id"], event["body_sha256"], event["actor"]) != (kind, question_id, digest, actor):
            raise ValueError("idempotency conflict: key was used with a different action, question, text or actor")
        return event["receipt"]
    if task["status"] not in store.OPEN:
        raise ValueError(f"task {task_id} is closed")
    state = task_questions.question_state(con, task)
    if not state["question"] or state["question"]["id"] != question_id or state["processing"] != "awaiting_decision":
        raise ValueError("stale question: it must be this task's latest unanswered question")
    return None


def submit(con, rt, task_id, question_id, text, idempotency_key, *, actor="dashboard"):
    """Atomically accept once under the task lock, then flush through core.answer.

    A separate submission lock serializes receipt publication and retries without
    nesting the transport lock. Process death retains the provisional receipt;
    identical retries return it without attempting another send.
    """
    _validate(question_id, text, idempotency_key, actor)
    digest = hashlib.sha256(text.encode()).hexdigest()
    with store.task_delivery(con, [f"task-question:{task_id}"]):
        with store.task_delivery(con, [task_id]), store.immediate(con):
            saved = _checked(con, task_id, question_id, idempotency_key, digest, actor, "task_question_answer")
            if saved is not None:
                return saved
            answer_id = store.add_message(con, task_id, store.QUEUED, text)
            receipt = dict(status="queued", delivered=0, pending=store.pending_answer_count(con, task_id),
                           task_id=task_id, question_id=question_id, answer_message_id=answer_id, actor=actor)
            event = dict(question_id=question_id, actor=actor, idempotency_key=idempotency_key,
                         body_sha256=digest, receipt=receipt)
            event_id = store.add_message(con, task_id, "task_question_answer", json.dumps(event))
        try:
            result = core.answer(con, rt, task_id, flush=True)
        except Exception as error:
            result = dict(status="failed", delivered=0, pending=store.pending_answer_count(con, task_id),
                          error=f"{type(error).__name__}: {error}"[:500])
        receipt.update(result)
        with store.immediate(con):
            con.execute("UPDATE messages SET body=? WHERE id=? AND kind='task_question_answer'",
                        (json.dumps(event), event_id))
        return receipt


def dismiss(con, task_id, question_id, reason, idempotency_key, *, actor="dashboard"):
    """Dismiss once; audit and inbox event commit together. No worker send or task status change."""
    _validate(question_id, reason, idempotency_key, actor)
    digest = hashlib.sha256(reason.encode()).hexdigest()
    with store.task_delivery(con, [f"task-question:{task_id}"]):
        with store.task_delivery(con, [task_id]), store.immediate(con):
            saved = _checked(con, task_id, question_id, idempotency_key, digest, actor, "question_dismissed")
            if saved is not None:
                return saved
            created_at = time.time()
            receipt = dict(status="dismissed", task_id=task_id, question_id=question_id, actor=actor,
                           reason=reason, created_at=created_at, pending=store.pending_answer_count(con, task_id))
            event = dict(question_id=question_id, actor=actor, reason=reason, created_at=created_at,
                         idempotency_key=idempotency_key, body_sha256=digest, receipt=receipt)
            event_id = store.add_message(con, task_id, "question_dismissed", json.dumps(event))
            receipt["message_id"] = event_id
            con.execute("UPDATE messages SET body=? WHERE id=?", (json.dumps(event), event_id))
            return receipt
