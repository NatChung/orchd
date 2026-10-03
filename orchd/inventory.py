"""Read-only Orch registry and runtime inventory, independent of open tasks."""
from . import store
from .orch_health import owner_health


def list_orchs(con, rt):
    try:
        jobs = rt.live_jobs()
    except Exception:
        jobs = None
    rows = store.list_orchs(con)
    counts = dict(registered=len(rows), alive=0, dead=0, unknown=0)
    orchs = []
    for row in rows:
        health = owner_health(row, jobs)
        counts[health["state"]] += 1
        orchs.append(dict(orch_id=row["id"], kind=row["kind"], created_at=row["created_at"],
                          stopped_at=row["stopped_at"], health=health["state"],
                          health_reason=health["reason"], open_task_count=row["open_task_count"]))
    # These are background sessions, not proven Orchs. Never expose prompts, paths or sockets.
    valid = isinstance(jobs, dict) and all(
        isinstance(k, str) and k and isinstance(v, dict) for k, v in jobs.items())
    unidentified = None
    if valid:
        known = store.known_job_ids(con)
        unidentified = [dict(job_id=job, health="alive") for job in sorted(jobs)
                        if job not in known and owner_health(
                            dict(kind="claude", job_id=job), jobs)["state"] == "alive"]
    return dict(orchs=orchs, counts=counts, unidentified_claude_sessions=unidentified)
