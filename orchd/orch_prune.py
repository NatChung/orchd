"""Stop and archive idle Orch jobs that have no obligations."""
from contextlib import ExitStack
from pathlib import Path
import time
import tomllib

from . import store
from .orch_health import owner_health

BATCH_MIN_AGE_SECONDS = 600


def _bound(con, table, predicate, orch_id):
    present = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    return bool(present and con.execute(f"SELECT 1 FROM {table} WHERE {predicate} LIMIT 1", (orch_id,)).fetchone())


def _obligation_reasons(con, row, external_owners=frozenset(), created_before=None):
    orch_id = row["id"]
    reasons = []
    if created_before is not None and row["created_at"] > created_before:
        reasons.append("recently_created")
    if orch_id in external_owners:
        reasons.append("external_listener_binding")
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



def listener_owners(path):
    """Read effective legacy listener configurations named by an Operator."""
    with Path(path).open("rb") as stream:
        manifest = tomllib.load(stream)
    bound_owners = manifest.get("line_bound_owners", [])
    if not isinstance(bound_owners, list) or not all(
        isinstance(owner, str) and owner.strip() and owner == owner.strip() and owner != "@bound"
        for owner in bound_owners
    ):
        raise ValueError("line_bound_owners must be a list of resolved Orch IDs")
    owners = set(bound_owners)
    for key in ("slack_configs", "line_configs"):
        paths = manifest.get(key)
        if not isinstance(paths, list) or not all(isinstance(item, str) and Path(item).is_absolute() for item in paths):
            raise ValueError(f"{key} must be a list of absolute config paths")
        for item in paths:
            with Path(item).open("rb") as stream:
                data = tomllib.load(stream)
            if key == "slack_configs":
                accounts = data.get("accounts")
                if not isinstance(accounts, list) or not all(isinstance(account, dict) for account in accounts):
                    raise ValueError("Slack listener config must contain an accounts list")
                values = [account["orchd_orch_id"] for account in accounts if "orchd_orch_id" in account]
            else:
                runner = data.get("runner")
                projects = runner.get("owners") if isinstance(runner, dict) else None
                if not isinstance(projects, dict):
                    raise ValueError("LINE listener config must contain a runner.owners table")
                values = list(projects.values())
            if not all(isinstance(owner, str) and owner.strip() and owner == owner.strip() for owner in values):
                raise ValueError("legacy listener owner IDs must be non-empty strings")
            # LINE resolves @bound at startup. Its cached owner may differ from today's desktop row.
            if "@bound" in values and (key != "line_configs" or not bound_owners):
                raise ValueError("resolve running LINE @bound owners in line_bound_owners before cleanup")
            owners.update(set(values) - {"@bound"})
    return owners


def _apply(con, rt, orch_id, routing_config, created_before=None):
    with store.task_delivery(con, [f"orch:{orch_id}"]):
        jobs = rt.live_jobs(timeout=10)
        row = store.get_orch(con, orch_id)
        if row is None:
            raise ValueError(f"unknown Orch {orch_id}")
        runtime_reasons = _runtime_reasons(rt, row, jobs) if row["kind"] == "claude" else ["unsupported_kind"]
        external_owners = listener_owners(routing_config) if routing_config else frozenset()
        with store.immediate(con):
            current = store.get_orch(con, orch_id)
            if dict(current) != dict(row):
                return dict(orch_id=orch_id, status="retained", reasons=["orch_changed"])
            reasons = _obligation_reasons(con, current, external_owners, created_before) or runtime_reasons
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


def _preview(con, rt, orch_id, external_owners, created_before=None):
    rows = [store.get_orch(con, orch_id)] if orch_id is not None else store.list_orchs(con)
    if orch_id is not None and rows[0] is None:
        raise ValueError(f"unknown Orch {orch_id}")
    jobs = rt.live_jobs(timeout=10)
    result = []
    for row in rows:
        reasons = _obligation_reasons(con, row, external_owners, created_before) or _runtime_reasons(rt, row, jobs)
        result.append(dict(orch_id=row["id"], status="retained" if reasons else "eligible", reasons=reasons))
    return result


def run(con, rt, orch_id=None, *, apply=False, routing_config=None):
    created_before = time.time() - BATCH_MIN_AGE_SECONDS if orch_id is None else None
    if not apply:
        owners = listener_owners(routing_config) if routing_config else frozenset()
        return _preview(con, rt, orch_id, owners, created_before)
    if orch_id is not None:
        return [_apply(con, rt, orch_id, routing_config)]
    if routing_config is None:
        raise ValueError("batch cleanup requires --routing-config")

    with ExitStack() as locks:
        try:
            locks.enter_context(store.task_delivery(con, ["orch-prune-batch"], timeout=0))
        except TimeoutError:
            return [dict(orch_id=None, status="skipped", reasons=["batch_already_running"])]
        preview = _preview(con, rt, None, listener_owners(routing_config), created_before)
        for index, item in enumerate(preview):
            if item["status"] == "eligible":
                preview[index] = _apply(con, rt, item["orch_id"], routing_config, created_before)
        return preview
