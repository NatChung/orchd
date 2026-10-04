"""Orch inventory, reversible archival and foreground selection (#71)."""
import time
from contextlib import nullcontext

from . import orch_revive, store
from .orch_health import owner_health, session_health


def obligations(con, orch_id):
    """Count links that must survive even after a task is closed or adopted."""
    marks = ",".join("?" * len(store.ORCH_KINDS))
    messages = con.execute(
        "SELECT COUNT(*) FROM messages m JOIN tasks t ON t.id=m.task_id WHERE "
        "((COALESCE(m.recipient_orch,t.orch_thread)=? AND "
        f"((m.read_at IS NULL AND m.kind IN ({marks})) OR m.wake_error IS NOT NULL "
        "OR (m.kind='question' AND NOT EXISTS (SELECT 1 FROM messages a "
        "WHERE a.task_id=m.task_id AND a.kind='answer' AND a.id>m.id)))) "
        "OR (COALESCE(m.notice_recipient,m.recipient_orch,CASE WHEN json_valid(m.evidence) "
        "THEN json_extract(m.evidence,'$.old_orch') END)=? AND "
        "(m.notice_error IS NOT NULL OR (m.kind='adopt_notice' AND m.wake_error IS NOT NULL))))",
        (orch_id, *store.ORCH_KINDS, orch_id)).fetchone()[0]
    entries = con.execute("SELECT COUNT(*) FROM entries WHERE orch_id=?", (orch_id,)).fetchone()[0]
    pending = con.execute(
        "SELECT COUNT(*) FROM entry_messages WHERE orch_id=? AND "
        "(read_at IS NULL OR delivery!='delivered' OR question_state IN ('current','queued'))",
        (orch_id,)).fetchone()[0]
    return dict(notification_count=messages, entry_pending_count=pending, binding_count=entries)


def _observe(con, row, health, attached, now):
    """Only successful dead probes count. No stop timestamp or historical row is deleted."""
    if health == "dead":
        first = row["first_seen_dead"]
        archived = row["archived_at"]
        if first is not None and now - first >= 3600 and not attached:
            archived = archived or now
        # An obligation discovered later makes an archived row visible to the reminder again.
        if attached:
            archived = None
        con.execute("UPDATE orchs SET first_seen_dead=?,last_verified_dead=?,archived_at=? WHERE id=?",
                    (now if first is None else first, now, archived, row["id"]))
    elif row["first_seen_dead"] is not None or (health in ("alive", "idle") and row["archived_at"] is not None):
        con.execute("UPDATE orchs SET first_seen_dead=NULL, archived_at=? WHERE id=?",
                    (None if health in ("alive", "idle") else row["archived_at"], row["id"]))


def list_orchs(con, rt, *, observe=False, now=None):
    rows = store.list_orchs(con)
    try:
        jobs = rt.live_jobs()
    except Exception:
        jobs = None
    counts = dict(registered=0, alive=0, idle=0, dead=0, unknown=0)
    orchs = []
    # Snapshot and finish every runtime/socket probe before acquiring the write lock.
    probes = {row["id"]: (dict(row), session_health(row, jobs, rt)) for row in rows}
    # Re-read under the lock: identity, archival and task changes invalidate old probes.
    with store.immediate(con) if observe else nullcontext():
        if observe:
            rows = store.list_orchs(con)
        counts["registered"] = len(rows)
        for row in rows:
            snapshot, health = probes.get(row["id"], (None, None))
            unchanged = snapshot == dict(row)
            if not unchanged:
                health = {"state": "unknown", "reason": "registry_changed"}
            links = obligations(con, row["id"])
            attached = row["open_task_count"] > 0 or any(links.values())
            if observe and unchanged:
                _observe(con, row, health["state"], attached, time.time() if now is None else now)
                current = store.get_orch(con, row["id"])
            else:
                current = row
            counts[health["state"]] += 1
            orchs.append(dict(orch_id=row["id"], kind=row["kind"], created_at=row["created_at"],
                              stopped_at=row["stopped_at"], health=health["state"],
                              health_reason=health["reason"], open_task_count=row["open_task_count"],
                              archived=current["archived_at"] is not None,
                              archived_at=current["archived_at"], first_seen_dead=current["first_seen_dead"],
                              last_verified_dead=current["last_verified_dead"], has_obligations=attached, **links))
    valid = isinstance(jobs, dict) and all(
        isinstance(k, str) and k and isinstance(v, dict) for k, v in jobs.items())
    unidentified = None
    if valid:
        known = store.known_job_ids(con)
        unidentified = [dict(job_id=job, health="alive") for job in sorted(jobs)
                        if job not in known and owner_health(
                            dict(kind="claude", job_id=job), jobs)["state"] == "alive"]
    return dict(orchs=orchs, counts=counts, unidentified_claude_sessions=unidentified)


def render(report, *, all=False):
    rows = report["orchs"] if all else [r for r in report["orchs"]
                                                   if r["health"] in ("alive", "idle") and not r["archived"]]
    lines = [f"{r['orch_id']}  {r['kind']}  {r['health']} ({r['health_reason']})"
             f"  tasks={r['open_task_count']}" + ("  archived" if r["archived"] else "") for r in rows]
    if not lines:
        lines.append("No live or resumable Orchs.")
    if not all:
        dead = sum(r["health"] == "dead" and r["has_obligations"] for r in report["orchs"])
        if dead:
            lines.append(f"{dead} dead Orch(s) still have tasks, notifications, questions or bindings → orchd adopt")
        unknown = report["counts"]["unknown"]
        if unknown:
            lines.append(f"{unknown} Orch(s) have unknown health (including unverified Codex) → orchd orchs --all")
    return "\n".join(lines)


def restore(con, orch_id):
    if store.get_orch(con, orch_id) is None:
        raise ValueError(f"unknown orch {orch_id}")
    con.execute("UPDATE orchs SET archived_at=NULL,first_seen_dead=NULL,last_verified_dead=NULL WHERE id=?",
                (orch_id,))


def attach(con, rt, orch_id, *, viewer=False):
    """Revalidate the selected identity; resume only its saved conversation when idle."""
    orch = store.get_orch(con, orch_id)
    if orch is None or orch["kind"] != "claude":
        raise ValueError(f"orch {orch_id} is not a registered Claude Orch")
    try:
        health = session_health(orch, rt.live_jobs(), rt)
    except Exception:
        health = {"state": "unknown", "reason": "runtime_unavailable"}
    if health["state"] == "idle":
        print(f"Resuming Orch {orch_id}'s saved conversation before attach.", flush=True)
        orch = orch_revive.revive(con, rt, orch_id, seen_job=orch["job_id"])
    elif health["state"] != "alive":
        raise ValueError(f"orch {orch_id} is {health['state']} ({health['reason']}); cannot attach")
    # Re-read under the same lock as revive, compare the selected row, and probe again.
    with store.task_delivery(con, [f"orch:{orch_id}"]):
        current = store.get_orch(con, orch_id)
        if any(current[k] != orch[k] for k in ("kind", "job_id", "session_id", "socket", "stopped_at")):
            raise ValueError(f"orch {orch_id} changed during selection; select it again")
        try:
            health = session_health(current, rt.live_jobs(), rt)
        except Exception:
            health = {"state": "unknown", "reason": "runtime_unavailable"}
        if health["state"] != "alive":
            raise ValueError(f"orch {orch_id} is {health['state']} ({health['reason']}); cannot attach")
        restore(con, orch_id)
        (rt.open_viewer if viewer else rt.attach)(current["job_id"])
