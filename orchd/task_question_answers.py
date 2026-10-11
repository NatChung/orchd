"""Durable dashboard submissions; transport remains core.answer's responsibility."""
import hashlib
import json
import time

from . import core, store, task_questions


def latest(con, task_id):
    row = con.execute("SELECT question_id,answer_message_id,actor,receipt,created_at FROM task_question_answers "
                      "WHERE task_id=? ORDER BY answer_message_id DESC LIMIT 1", (task_id,)).fetchone()
    return dict(row, receipt=json.loads(row["receipt"])) if row else None


def submit(con, rt, task_id, question_id, text, idempotency_key, *, actor="dashboard"):
    """Accept once under the task lock, then flush through core.answer.

    A separate submission lock serializes receipt publication and retries without
    nesting the transport lock. Acceptance, queue, provisional receipt and inbox
    event commit atomically before any send. After process death a retry returns
    that saved receipt and never flushes again. The regular answer/auto-flush path
    can still deliver accepted queue rows. Receipts describe this submission call,
    not later worker execution or flushes.
    """
    if not isinstance(question_id, int) or isinstance(question_id, bool) or question_id < 1:
        raise ValueError("question id must be a positive integer")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("answer text must be non-empty")
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        raise ValueError("idempotency key must be non-empty")
    if actor not in ("dashboard", "operator"):
        raise ValueError("actor must be dashboard or operator")
    digest = hashlib.sha256(text.encode()).hexdigest()
    with store.task_delivery(con, [f"task-question:{task_id}"]):
        with store.task_delivery(con, [task_id]), store.immediate(con):
            task = store.get_task(con, task_id)
            keyed = con.execute("SELECT * FROM task_question_answers WHERE task_id=? AND idempotency_key=?",
                                (task_id, idempotency_key)).fetchone()
            if keyed:
                if (keyed["question_id"], keyed["body_sha256"], keyed["actor"]) != (question_id, digest, actor):
                    raise ValueError("idempotency conflict: key was used with a different question, text or actor")
                # A retry recovers history even after the task is closed or asks a newer question.
                return json.loads(keyed["receipt"])
            if task["status"] not in store.OPEN:
                raise ValueError(f"task {task_id} is closed")
            state = task_questions.question_state(con, task)
            if not state["question"] or state["question"]["id"] != question_id or state["processing"] != "awaiting_decision":
                raise ValueError("stale question: it must be this task's latest unanswered question")
            answer_id = store.add_message(con, task_id, store.QUEUED, text)
            receipt = dict(status="queued", delivered=0, pending=store.pending_answer_count(con, task_id),
                           task_id=task_id, question_id=question_id, answer_message_id=answer_id, actor=actor)
            event_id = store.add_message(con, task_id, "task_question_answer", json.dumps(receipt))
            con.execute("INSERT INTO task_question_answers(task_id,idempotency_key,question_id,answer_message_id,"
                        "actor,body_sha256,receipt,event_id,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (task_id, idempotency_key, question_id, answer_id, actor, digest,
                         json.dumps(receipt), event_id, time.time()))
        try:
            result = core.answer(con, rt, task_id, flush=True)
        except Exception as error:
            result = dict(status="failed", delivered=0, pending=store.pending_answer_count(con, task_id),
                          error=f"{type(error).__name__}: {error}"[:500])
        receipt.update(result)
        with store.immediate(con):
            con.execute("UPDATE task_question_answers SET receipt=? WHERE task_id=? AND idempotency_key=?",
                        (json.dumps(receipt), task_id, idempotency_key))
            con.execute("UPDATE messages SET body=? WHERE id=? AND kind='task_question_answer'",
                        (json.dumps(receipt), event_id))
        return receipt
