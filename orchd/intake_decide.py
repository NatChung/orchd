"""Orch decisions on intake receipts: one append-only ledger from receipt to issue, goal, task or question.

An Orch records one of six actions for a receipt it owns. attach_existing, record and ignore finish in the same
transaction (and ack or exclude the receipt). create_issue, ask_nat and dispatch first record an *intent*, the
Orch performs the outward step (GitHub, Nat, a worker), then `resolve` supplies the read-back ref, task or
question; only then does the receipt get acked. A crash in between leaves an open intent that a retry returns
instead of repeating (no second ticket, question or worker). SQLite and GitHub are not one transaction: this is
recoverable and reconcilable, not exactly-once.

Every call is compare-and-set on the receipt's revision (the newest intake_log row touching it) and on the
owner, so a stale Orch or a second consumer is refused. Intake text is third-party data: it is never read here,
and `approval` is only a recorded reference, never evidence that Operator approved anything.
"""
import json
import re
import time
import uuid

from . import goals, intake, intake_bridge, issue_links, store

ACTIONS = ("attach_existing", "create_issue", "ask_nat", "dispatch", "record", "ignore")
IMMEDIATE = ("attach_existing", "record", "ignore")
OPEN_STATES = ("intent", "uncertain")
OUTCOMES = ("completed", "uncertain", "failed")
DECIDE_FIELDS = {"receipt_id", "revision", "action", "reason", "goal_id", "goal_na_reason", "issue_ref",
                 "issue_na_reason", "approval", "idempotency_key"}
RESOLVE_FIELDS = {"decision_id", "outcome", "issue_ref", "task_id", "question_id", "external_receipt", "detail",
                  "idempotency_key"}
GITHUB_REPO = re.compile(r"github:[^/]+/([^#]+)#")
IGNORE_RULE = "intake-decision-v1"


def _sha(payload):
    return intake._sha(json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")))


def _question_context(row):
    return "[orchd intake decision " + json.dumps(
        {key: row[key] for key in ("decision_id", "receipt_id", "issue_ref", "goal_id")},
        sort_keys=True, separators=(",", ":")) + "]"


def _view(row, status=None):
    out = {k: row[k] for k in row.keys() if k != "request_sha256"}
    out["trace"] = dict(receipt_id=row["receipt_id"], issue_ref=row["issue_ref"], goal_id=row["goal_id"],
                        task_id=row["task_id"], question_id=row["question_id"])
    if row["action"] == "ask_nat":
        out["question_context"] = _question_context(row)
    if status:
        out["status"] = status
    return out


def _by_key(con, key):
    return con.execute("SELECT * FROM intake_decisions WHERE idempotency_key=?", (key,)).fetchone()


def _decision(con, decision_id):
    row = con.execute("SELECT * FROM intake_decisions WHERE decision_id=?", (decision_id,)).fetchone()
    if row is None:
        raise intake.IntakeError("not_found", f"unknown decision {decision_id}")
    return row


def _check_keys(payload, allowed, name):
    if not isinstance(payload, dict):
        raise intake.IntakeError("invalid", f"{name} must be a JSON object")
    extra = sorted(set(payload) - allowed)
    if extra:
        raise intake.IntakeError("unknown_field", f"unknown {name} field(s): {', '.join(extra)}")


def _issue_ref(value, name="issue_ref"):
    if not (isinstance(value, str) and intake.ISSUE_REF.fullmatch(value)):
        raise intake.IntakeError("invalid", f"{name} must be github:owner/repo#N or jira:KEY-N")
    return value


def _either(payload, ref_key, na_key, label, *, required_ref=False):
    """A decision always carries a ref or a stated reason why none applies, never both and never neither."""
    ref, na = payload.get(ref_key), payload.get(na_key)
    if ref is not None and na is not None:
        raise intake.IntakeError("invalid", f"give {ref_key} or {na_key}, not both")
    if ref is None and na is None:
        raise intake.IntakeError("invalid", f"{label}: give {ref_key} or {na_key} with the reason it does not apply")
    if ref is None and required_ref:
        raise intake.IntakeError("invalid", f"{ref_key} is required for this action")
    if na is not None:
        intake._text(na, na_key, limit=300)
    return ref, na


def _repo_of(issue_ref):
    found = GITHUB_REPO.match(issue_ref or "")
    return found.group(1) if found else None


def _validate_decide(payload):
    _check_keys(payload, DECIDE_FIELDS, "decision")
    for key in ("receipt_id", "action", "reason", "idempotency_key"):
        intake._text(payload.get(key), key, limit=1000 if key == "reason" else 200)
    if payload["action"] not in ACTIONS:
        raise intake.IntakeError("invalid", f"action must be one of {', '.join(ACTIONS)}")
    if isinstance(payload.get("revision"), bool) or not isinstance(payload.get("revision"), int) or payload["revision"] < 0:
        raise intake.IntakeError("invalid", "revision must be the non-negative integer you read from show/pending")
    if payload.get("issue_ref") is not None:
        _issue_ref(payload["issue_ref"])
    intake._text(payload.get("goal_id"), "goal_id", limit=200, required=False)
    intake._text(payload.get("approval"), "approval", limit=300, required=False)
    action = payload["action"]
    if action == "create_issue":
        if payload.get("issue_ref") is not None:
            raise intake.IntakeError("invalid", "issue_ref must be empty here; resolve supplies it after read-back")
        intake._text(payload.get("issue_na_reason"), "issue_na_reason", limit=300, required=False)
    else:
        _either(payload, "issue_ref", "issue_na_reason", "issue", required_ref=action in ("attach_existing", "dispatch"))
    _either(payload, "goal_id", "goal_na_reason", "goal", required_ref=action == "dispatch")
    if action == "dispatch" and not payload["issue_ref"].startswith("github:"):
        raise intake.IntakeError("invalid", "dispatch needs a github issue_ref in the same repo as the goal")


def _complete_receipt(con, row, decision):
    """Inside the caller's transaction: the receipt reaches its terminal state for this decision."""
    now = time.time()
    if decision["issue_ref"] is not None:
        con.execute("UPDATE intake_events SET issue_ref=? WHERE receipt_id=?", (decision["issue_ref"], row["receipt_id"]))
    if decision["action"] == "ignore":
        con.execute("UPDATE intake_events SET state='excluded',excluded_at=?,excluded_by='orch',exclusion_reason=?,"
                    "exclusion_rule_version=? WHERE receipt_id=?",
                    (now, f"decision {decision['decision_id']}", IGNORE_RULE, row["receipt_id"]))
        intake._log(con, "exclude", row["receipt_id"])
    else:
        con.execute("UPDATE intake_events SET state='acked',acked_at=?,acked_by='orch' WHERE receipt_id=?",
                    (now, row["receipt_id"]))
        intake._log(con, "ack", row["receipt_id"])
    con.execute("UPDATE intake_notifications SET state='withdrawn' WHERE receipt_id=? AND state IN ('pending','failed')",
                (row["receipt_id"],))


def _check_claim(con, decision):
    # All receipt mutations except this ledger's own progress invalidate the claim.
    changed = con.execute("SELECT 1 FROM intake_log WHERE ref=? AND seq>? "
                          "AND kind NOT IN ('decision','decision_uncertain') LIMIT 1",
                          (decision["receipt_id"], decision["revision"])).fetchone()
    if changed:
        raise intake.IntakeError("stale", "receipt changed since claim; reconcile and terminate before deciding again")


def decide(con, payload, *, orch_id):
    """Record a decision as the Orch `orch_id` (over MCP this is the caller's own thread id, never an argument)."""
    _validate_decide(payload)
    action = payload["action"]
    request = _sha(payload)
    with store.immediate(con):
        old = _by_key(con, payload["idempotency_key"])
        if old is not None:
            if old["request_sha256"] != request or old["owner_orch"] != orch_id:
                raise intake.IntakeError("idempotency_conflict", "idempotency_key was used for a different decision")
            if old["state"] in OPEN_STATES:
                if intake._event(con, old["receipt_id"])["owner_orch"] != orch_id:
                    raise intake.IntakeError("not_owner", "receipt owner changed")
                _check_claim(con, old)
            return _view(old, "duplicate")
        row = intake._event(con, payload["receipt_id"])
        if row["state"] != "received":
            raise intake.IntakeError("invalid_state", f"an {row['state']} receipt cannot take a decision")
        if row["owner_orch"] != orch_id:
            raise intake.IntakeError("not_owner", "this receipt is not owned by you (unassigned or rebound); "
                                     "re-read pending and decide only what you own")
        open_ = con.execute("SELECT * FROM intake_decisions WHERE receipt_id=? AND state IN ('intent','uncertain') "
                            "ORDER BY created_at DESC LIMIT 1", (row["receipt_id"],)).fetchone()
        if open_ is not None:
            _check_claim(con, open_)
            if open_["action"] == action and open_["owner_orch"] == orch_id:
                return _view(open_, "pending")  # same intent again: never a second ticket, question or worker
            raise intake.IntakeError("already_claimed", f"decision {open_['decision_id']} ({open_['action']}) is "
                                     "unresolved; resolve it first")
        current = intake_bridge.receipt_revision(con, row["receipt_id"])
        if payload["revision"] != current:
            raise intake.IntakeError("stale", f"receipt changed (revision {current}); read it again before deciding")
        if payload.get("goal_id") is not None:
            try:
                goal = goals.get(con, payload["goal_id"])
            except KeyError:
                raise intake.IntakeError("invalid", f"unknown goal {payload['goal_id']}") from None
            if action == "dispatch" and _repo_of(payload["issue_ref"]) != goal["repo"]:
                raise intake.IntakeError("invalid", "issue_ref and goal must belong to the same repo")
        decision_id = "id_" + uuid.uuid4().hex[:12]
        na_issue = payload.get("issue_na_reason") or ("pending_create" if action == "create_issue" else None)
        now = time.time()
        con.execute(
            "INSERT INTO intake_decisions(decision_id,receipt_id,revision,action,actor,owner_orch,reason,goal_id,"
            "goal_na_reason,issue_ref,issue_na_reason,approval,state,request_sha256,idempotency_key,created_at) "
            "VALUES(?,?,?,?,'orch',?,?,?,?,?,?,?,'intent',?,?,?)",
            (decision_id, row["receipt_id"], payload["revision"], action, orch_id, payload["reason"],
             payload.get("goal_id"), payload.get("goal_na_reason"), payload.get("issue_ref"), na_issue,
             payload.get("approval"), request, payload["idempotency_key"], now))
        intake._log(con, "decision", row["receipt_id"])
        if action in IMMEDIATE:
            decision = _decision(con, decision_id)
            _complete_receipt(con, row, decision)
            con.execute("UPDATE intake_decisions SET state='completed',completed_at=? WHERE decision_id=?",
                        (now, decision_id))
            intake._log(con, "decision_complete", row["receipt_id"])
        return _view(_decision(con, decision_id), "decided" if action in IMMEDIATE else "intent")


def _external(value):
    if value is None:
        return None
    if not isinstance(value, str):
        value = json.dumps(value, sort_keys=True, ensure_ascii=False)
    return intake._text(value, "external_receipt", limit=1000)


def resolve(con, payload, *, orch_id):
    """Finish (or report the state of) an open intent after the outward step. `completed` verifies the
    evidence, completes the decision and acks the receipt in one transaction; `uncertain` and `failed` never ack."""
    _check_keys(payload, RESOLVE_FIELDS, "resolution")
    decision_id = intake._text(payload.get("decision_id"), "decision_id", limit=200)
    outcome = payload.get("outcome", "completed")
    if outcome not in OUTCOMES:
        raise intake.IntakeError("invalid", f"outcome must be one of {', '.join(OUTCOMES)}")
    detail = intake._text(payload.get("detail"), "detail", limit=500, required=False)
    external = _external(payload.get("external_receipt"))
    if payload.get("issue_ref") is not None:
        _issue_ref(payload["issue_ref"])
    with store.immediate(con):
        decision = _decision(con, decision_id)
        row = intake._event(con, decision["receipt_id"])
        if row["owner_orch"] != orch_id:
            raise intake.IntakeError("not_owner", "only the current receipt owner can reconcile")
        if outcome == "failed" and decision["state"] in OPEN_STATES:
            if not detail:
                raise intake.IntakeError("invalid", "termination requires reconciliation detail")
            con.execute("UPDATE intake_decisions SET state='failed',external_receipt=? WHERE decision_id=?",
                        (json.dumps(dict(reconciled_by=orch_id, detail=detail, evidence=external)), decision_id))
            intake._log(con, "decision_failed", row["receipt_id"])
            return _view(_decision(con, decision_id), "failed")
        if decision["owner_orch"] != orch_id:
            raise intake.IntakeError("not_owner", "only the deciding Orch can resolve its decision")
        if decision["state"] == "completed":
            same = (decision["issue_ref"] in (None, payload.get("issue_ref")) and
                    decision["task_id"] in (None, payload.get("task_id")) and
                    decision["question_id"] in (None, payload.get("question_id")))
            if outcome != "completed" or not same:
                raise intake.IntakeError("conflict", "decision already completed with different evidence")
            return _view(decision, "duplicate")
        if decision["state"] == "failed":
            raise intake.IntakeError("invalid_state", "decision failed; record a new decision")
        row = intake._event(con, decision["receipt_id"])
        if row["state"] != "received" or row["owner_orch"] != orch_id:
            raise intake.IntakeError("stale", "the receipt is no longer yours to resolve")
        _check_claim(con, decision)
        if outcome != "completed":
            con.execute("UPDATE intake_decisions SET state=?,external_receipt=COALESCE(?,external_receipt) "
                        "WHERE decision_id=?", (outcome, external or detail, decision_id))
            intake._log(con, "decision_" + outcome, row["receipt_id"])
            return _view(_decision(con, decision_id), outcome)
        fields = _evidence(con, decision, payload, external, orch_id)
        con.execute("UPDATE intake_decisions SET state='completed',completed_at=?,external_receipt=?,"
                    "issue_ref=COALESCE(?,issue_ref),issue_na_reason=CASE WHEN ? IS NULL THEN issue_na_reason END,"
                    "task_id=?,question_id=? WHERE decision_id=?",
                    (time.time(), external, fields["issue_ref"], fields["issue_ref"], fields["task_id"],
                     fields["question_id"], decision_id))
        _complete_receipt(con, row, _decision(con, decision_id))
        intake._log(con, "decision_complete", row["receipt_id"])
        return _view(_decision(con, decision_id), "completed")


def _evidence(con, decision, payload, external, orch_id):
    action = decision["action"]
    out = dict(issue_ref=None, task_id=None, question_id=None)
    if action == "create_issue":
        out["issue_ref"] = _issue_ref(payload.get("issue_ref"))
        if external is None:
            raise intake.IntakeError("invalid", "create_issue needs external_receipt: the read-back of the created issue")
    elif action == "ask_nat":
        question = payload.get("question_id")
        if isinstance(question, bool) or not isinstance(question, int):
            raise intake.IntakeError("invalid", "ask_nat needs question_id of the question you sent")
        found = con.execute("SELECT orch_id,direction,question_state,body,issue_ref,created_at FROM entry_messages WHERE id=?",
                            (question,)).fetchone()
        if found is None or found["direction"] != "out" or found["question_state"] is None or found["orch_id"] != orch_id:
            raise intake.IntakeError("invalid", "question_id is not a question your Orch sent to Nat")
        if (found["issue_ref"] != decision["issue_ref"] or found["created_at"] < decision["created_at"]
                or _question_context(decision) not in found["body"]):
            raise intake.IntakeError("invalid", "question must include this decision's question_context and matching issue_ref")
        out["question_id"] = question
    elif action == "dispatch":
        task_id = intake._text(payload.get("task_id"), "task_id", limit=200)
        try:
            task = store.get_task(con, task_id)
        except KeyError:
            raise intake.IntakeError("invalid", f"unknown task {task_id}") from None
        history = issue_links.history(con, task_id)
        linked = history[-1]["ref"] if history else None
        if task["orch_thread"] != orch_id or task["goal_id"] != decision["goal_id"] or linked != decision["issue_ref"]:
            raise intake.IntakeError("invalid", "task must be yours, on the decided goal, linked to the decided issue_ref")
        out["task_id"] = task_id
    else:
        raise intake.IntakeError("invalid_state", f"{action} completes at decide; nothing to resolve")
    return out


def decisions(con, receipt_id=None, *, orch_id=None, state=None):
    """Read-only trace: receipt -> decision -> issue / goal / task / question, with actor, reason and state."""
    clauses, params = [], []
    for column, value in (("receipt_id", receipt_id), ("owner_orch", orch_id), ("state", state)):
        if value is not None:
            clauses.append(f"{column}=?")
            params.append(value)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = con.execute(f"SELECT * FROM intake_decisions{where} ORDER BY created_at,decision_id", params).fetchall()
    return dict(status="decisions", decisions=[_view(r) for r in rows])


def retention_blocked(con, receipt_id):
    """A receipt with an unfinished decision must not be purged or labeled expired by any retention policy."""
    return con.execute("SELECT 1 FROM intake_decisions WHERE receipt_id=? AND state IN ('intent','uncertain') LIMIT 1",
                       (receipt_id,)).fetchone() is not None


def backlog(con, *, now=None, owner=None):
    """Read-only operational view: queues by owner (oldest age), unassigned, notification trouble, open decisions."""
    now = time.time() if now is None else now
    queues = []
    for row in con.execute("SELECT owner_orch,COUNT(*) n,MIN(received_at) oldest FROM intake_events "
                           "WHERE state='received' GROUP BY owner_orch ORDER BY owner_orch"):
        if owner is None or row["owner_orch"] == owner:
            queues.append(dict(owner=row["owner_orch"], waiting=row["n"],
                               owner_state="assigned" if row["owner_orch"] else "unassigned",
                               oldest_age_seconds=round(now - row["oldest"], 1)))
    notify = {r["state"]: r["n"] for r in con.execute(
        "SELECT state,COUNT(*) n FROM intake_notifications" + (" WHERE owner_orch=?" if owner else "")
        + " GROUP BY state", (owner,) if owner else ())}
    failing = [dict(notification_id=r["id"], state=r["state"], receipt_id=r["receipt_id"], owner=r["owner_orch"], attempts=r["attempts"], error=r["error"])
               for r in con.execute("SELECT * FROM intake_notifications WHERE state IN ('failed','uncertain') ORDER BY id")
               if owner is None or r["owner_orch"] == owner]
    open_decisions = [dict(decision_id=r["decision_id"], receipt_id=r["receipt_id"], action=r["action"],
                           state=r["state"], owner=r["owner_orch"], age_seconds=round(now - r["created_at"], 1))
                      for r in con.execute("SELECT d.*,e.owner_orch AS current_owner FROM intake_decisions d "
                                           "JOIN intake_events e USING(receipt_id) WHERE d.state IN ('intent','uncertain') "
                                           "ORDER BY d.created_at")
                      if owner is None or r["current_owner"] == owner]
    return dict(status="backlog", queues=queues, notifications=notify, notification_failures=failing,
                open_decisions=open_decisions)
