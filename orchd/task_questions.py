"""Worker question state derived from committed task messages, independent of inbox reads."""
import json

from . import store


def question_state(con, task):
    """Latest question and subsequent resolution evidence. Message IDs define log order.

    Followup transport rows carry an instruction, not an answer. Only a structured
    committed acceptance/queue/delivery event can resolve a question via followup.
    A failed or unstructured followup never does so by itself.
    """
    question = con.execute("SELECT * FROM messages WHERE task_id=? AND kind='question' ORDER BY id DESC LIMIT 1",
                           (task["id"],)).fetchone()
    if question is None:
        return dict(question=None, processing="insufficient_data", evidence=[])
    evidence = []
    rows = list(con.execute("SELECT * FROM messages WHERE task_id=? AND id>? "
                            "AND kind IN ('answer','answer_queued','followup') ORDER BY id",
                            (task["id"], question["id"])))
    for row in rows:
        if row["kind"] == "followup":
            try:
                event = json.loads(row["body"])
            except (ValueError, TypeError):
                continue
            if not isinstance(event, dict) or not event.get("token") or not event.get("message"):
                continue
            status = event.get("status")
            if status not in ("accepted", "queued", "delivered"):
                continue
            resolution = "saved_pending_delivery" if status == "queued" else "resolved"
            # A queued followup's event is not rewritten by a later answer flush.
            # Its consumed queue/answer receipt proves it left the queue.
            if status == "queued" and any(
                    r["id"] > row["id"] and r["kind"] in ("answer", "answer_queued")
                    and r["body"].startswith("[followup] New instruction for this same task, not an answer to a question.")
                    and r["body"].partition("\n\n")[2] == event["message"]
                    and (r["kind"] == "answer" or r["read_at"] is not None) for r in rows):
                resolution = "resolved"
        else:
            if row["body"].startswith("[followup] New instruction for this same task, not an answer to a question."):
                continue
            resolution = ("saved_pending_delivery" if row["kind"] == "answer_queued" and row["read_at"] is None
                          else "resolved")
        evidence.append(dict(message_id=row["id"], kind=row["kind"], created_at=row["created_at"],
                             read_at=row["read_at"], resolution=resolution))
    processing = ("resolved" if any(e["resolution"] == "resolved" for e in evidence)
                  else "saved_pending_delivery" if evidence else "awaiting_decision")
    return dict(question=dict(question), processing=processing, evidence=evidence)


def pending(con, tasks):
    result = []
    for task in tasks:
        if task["status"] not in store.OPEN:
            continue
        state = question_state(con, task)
        question = state["question"]
        if state["processing"] == "resolved" or (question is None and task["status"] != "question"):
            continue
        result.append(dict(id=question["id"] if question else None, task_id=task["id"],
                           body=question["body"] if question else None,
                           created_at=question["created_at"] if question else None,
                           task=dict(repo=task["repo"], title=task["title"], status=task["status"],
                                     owner=task["orch_thread"]),
                           processing=state["processing"],
                           evidence=dict(latest_question_id=question["id"] if question else None,
                                         question_read_at=question["read_at"] if question else None,
                                         read_at_affects_pending=False, resolutions=state["evidence"],
                                         reason="missing_question_message" if question is None else
                                         "answer_saved" if state["evidence"] else "no_subsequent_resolution")))
    return result
