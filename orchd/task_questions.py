"""Worker question state derived from committed task messages, independent of inbox reads."""
import json

from . import question_summary, store
from .core import FOLLOWUP_HEAD


def followup_id(row):
    try:
        evidence = json.loads(row["evidence"] or "null")
    except ValueError:
        return None
    value = evidence.get("followup_id") if isinstance(evidence, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def dismissals(con, task_id=None):
    result = []
    for row in con.execute("SELECT * FROM messages WHERE kind='question_dismissed' "
                           + ("AND task_id=? " if task_id is not None else "") + "ORDER BY id",
                           (task_id,) if task_id is not None else ()):
        try:
            event = json.loads(row["body"])
        except ValueError:
            continue
        if (not isinstance(event, dict) or event.get("actor") not in ("dashboard", "operator")
                or not isinstance(event.get("question_id"), int) or isinstance(event.get("question_id"), bool)
                or not isinstance(event.get("reason"), str) or not event["reason"].strip()
                or not isinstance(event.get("receipt"), dict) or event["receipt"].get("status") != "dismissed"):
            continue
        question = con.execute("SELECT 1 FROM messages WHERE id=? AND task_id=? AND kind='question'",
                               (event["question_id"], row["task_id"])).fetchone()
        if question:
            result.append(dict(id=row["id"], task_id=row["task_id"], question_id=event["question_id"],
                               actor=event["actor"], reason=event["reason"], created_at=row["created_at"],
                               resolution="dismissed"))
    return result


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
    dismissed = [r for r in dismissals(con, task["id"]) if r["question_id"] == question["id"]
                 and r["id"] > question["id"]]
    if dismissed:
        return dict(question=dict(question), processing="dismissed", evidence=dismissed)
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
            # A failed/queued followup event is not rewritten by a later flush.
            # Match transport rows to a structured acceptance event; arbitrary
            # followups and their failed delivery alone never resolve a question.
            transport = [r for r in rows if
                    r["id"] > row["id"] and r["kind"] in ("answer", "answer_queued")
                    and followup_id(r) == row["id"]]
            delivered = [r for r in transport if r["kind"] == "answer" or r["read_at"] is not None]
            if status in ("accepted", "delivered") or delivered:
                resolution = "resolved"
            elif status == "queued" or transport:
                resolution = "saved_pending_delivery"
            else:
                continue
            evidence.extend(dict(message_id=r["id"], kind=r["kind"], created_at=r["created_at"],
                                 read_at=r["read_at"], resolution="resolved" if r in delivered else
                                 "saved_pending_delivery") for r in transport)
        else:
            if row["body"].startswith(FOLLOWUP_HEAD) or followup_id(row) is not None:
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
        if state["processing"] in ("resolved", "dismissed") or (question is None and task["status"] != "question"):
            continue
        result.append(dict(id=question["id"] if question else None, task_id=task["id"],
                           body=question["body"] if question else None,
                           summary=question_summary.parse(question["body"]) if question else None,
                           created_at=question["created_at"] if question else None,
                           task=dict(task, owner=task["orch_thread"]), task_open=True,
                           processing=state["processing"],
                           evidence=dict(latest_question_id=question["id"] if question else None,
                                         question_read_at=question["read_at"] if question else None,
                                         read_at_affects_pending=False, resolutions=state["evidence"],
                                         reason="missing_question_message" if question is None else
                                         "answer_saved" if state["evidence"] else "no_subsequent_resolution")))
    return result
