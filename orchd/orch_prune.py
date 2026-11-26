"""Stop and archive explicitly selected idle Orch jobs that have no obligations."""
import time

from . import store
from .orch_health import owner_health


def _bound(con, table, predicate, orch_id):
    present = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    return bool(present and con.execute(f"SELECT 1 FROM {table} WHERE {predicate} LIMIT 1", (orch_id,)).fetchone())


def _obligation_reasons(con, row):
    orch_id = row["id"]
    reasons = []
    if _bound(con, "entries", "orch_id=?", orch_id):
        reasons.append("desktop_binding")
    if _bound(con, "entry_messages", "orch_id=? AND ("
              "(kind='question' AND question_state IN ('current','queued','snoozed')) OR "
              "(kind IN ('message','reply') AND direction='in' AND read_at IS NULL AND delivery!='skipped') OR "
              "(kind='message' AND direction='out' AND delivery IN ('pending','sending','failed','held')))", orch_id):
        reasons.append("pending_entry_work")
    if _bound(con, "intake_events", "owner_orch=? AND state='received'", orch_id):
        reasons.append("pending_intake")
    if _bound(con, "interfaces", "orch_id=?", orch_id):
        reasons.append("listener_binding")
    if _bound(con, "channel_routes", "owner_orch=? AND active=1", orch_id):
        reasons.append("channel_binding")
    if _bound(con, "interface_requests", "orch_id=? AND reply IS NULL AND dismissed_at IS NULL", orch_id):
        reasons.append("pending_interface_request")
    if con.execute(f"SELECT 1 FROM tasks WHERE orch_thread=? AND status IN ({','.join('?' * len(store.OPEN))})",
                   (orch_id, *store.OPEN)).fetchone():
        reasons.append("open_tasks")
    if _bound(con, "workflow_runs", "orch_thread=? AND state IN "
              "('active','needs_recovery','blocked','awaiting_input','repairing','cancelling')", orch_id):
        reasons.append("pending_workflow")
    pending = "prune_requested_at" in row.keys() and row["prune_requested_at"] is not None
    if row["stopped_at"] is not None and not pending:
        reasons.append("already_stopped")
    elif row["kind"] != "claude":
        reasons.append("unsupported_kind")
    return reasons


def _runtime_reasons(rt, row, jobs):
    health = owner_health(row, jobs)
    if health["state"] == "unknown" or not row["socket"]:
        return ["runtime_unverified"]
    job = jobs.get(row["job_id"], {})
    if job.get("sessionId") and job["sessionId"] != row["session_id"]:
        return ["runtime_unverified"]
    listening = rt.socket_listening(row["socket"])
    if health["state"] == "alive":
        if listening is not True or job.get("status") != "idle":
            return ["orch_not_idle"]
    elif listening is not False:
        return ["runtime_unverified"]
    return []


def _apply(con, rt, orch_id):
    with store.task_delivery(con, [f"orch:{orch_id}"]):
        jobs = rt.live_jobs(timeout=10)
        row = store.get_orch(con, orch_id)
        if row is None:
            raise ValueError(f"unknown Orch {orch_id}")
        runtime_reasons = _runtime_reasons(rt, row, jobs) if row["kind"] == "claude" else ["unsupported_kind"]
        with store.immediate(con):
            current = store.get_orch(con, orch_id)
            if dict(current) != dict(row):
                return dict(orch_id=orch_id, status="retained", reasons=["orch_changed"])
            reasons = _obligation_reasons(con, current) or runtime_reasons
            if reasons:
                return dict(orch_id=orch_id, status="retained", reasons=reasons)
            now = time.time()
            con.execute("UPDATE orchs SET stopped_at=COALESCE(stopped_at,?),"
                        "prune_requested_at=COALESCE(prune_requested_at,?) WHERE id=?", (now, now, orch_id))

        # The short transaction above reserves the owner against new work. Keep the
        # per-Orch lock across the provider operation, never the SQLite writer lock.
        try:
            if owner_health(row, jobs)["state"] == "alive":
                rt.stop_worker(row["job_id"])
                jobs = rt.live_jobs(timeout=10)
            if owner_health(row, jobs)["state"] != "dead" or rt.socket_listening(row["socket"]) is not False:
                return dict(orch_id=orch_id, status="unconfirmed", reasons=["stop_not_verified"])
        except Exception:
            return dict(orch_id=orch_id, status="unconfirmed", reasons=["stop_not_verified"])
        now = time.time()
        con.execute("UPDATE orchs SET archived_at=?,first_seen_dead=COALESCE(first_seen_dead,?),"
                    "last_verified_dead=?,prune_requested_at=NULL WHERE id=?", (now, now, now, orch_id))
        return dict(orch_id=orch_id, status="stopped_and_archived", reasons=[])


def run(con, rt, orch_id=None, *, apply=False):
    if apply:
        if orch_id is None:
            raise ValueError("--apply requires --orch ID")
        return [_apply(con, rt, orch_id)]
    rows = [store.get_orch(con, orch_id)] if orch_id is not None else store.list_orchs(con)
    if orch_id is not None and rows[0] is None:
        raise ValueError(f"unknown Orch {orch_id}")
    jobs = rt.live_jobs(timeout=10)
    result = []
    for row in rows:
        reasons = _obligation_reasons(con, row) or _runtime_reasons(rt, row, jobs)
        result.append(dict(orch_id=row["id"], status="retained" if reasons else "eligible", reasons=reasons))
    return result
