"""Bring back a Claude Orch that Claude Code's daemon retired for idling (#72).

The daemon retires a background session after about an hour at an idle prompt, and its socket goes with it.
Retired is not stopped: `stopped_at` is set only by `orchd orch stop`, and a stopped Orch is never revived.
A revive resumes the same conversation (`claude --bg --resume`). Claude runs it as a new job and session, so
the row's job_id and session_id change; the orch_id, socket path and Desktop binding stay.
"""
from . import paths, store
from .orch_health import owner_health

REVIVABLE = ("job_absent", "job_failed", "job_retired")  # owner_health reasons that mean the job ended, not that we cannot tell
GONE = (FileNotFoundError, ConnectionRefusedError)  # what connecting to a retired Orch's socket raises


def revivable(orch):
    return (orch is not None and orch["kind"] == "claude" and orch["stopped_at"] is None
            and bool(orch["session_id"]))


def retired(rt, orch, health):
    """The job a revive should replace, or None. `claude agents` keeps listing a retired job (no pid, no status)
    so missing transport alone must never retire a live process."""
    if not revivable(orch):
        return None
    if health["state"] == "dead":
        return orch["job_id"] if health["reason"] in REVIVABLE else None
    return None


def revive(con, rt, orch_id, seen_job=None):
    """Resume a retired Orch and return its row.

    Without `seen_job` it revives only when `claude agents` confirms the job ended. With `seen_job`, a concurrent replacement is left alone; fresh provider death
    evidence is still required. A missing socket never proves an alive process retired.
    The orch lock serializes revives; whoever gets it second finds a new job_id and returns that row.
    """
    with store.task_delivery(con, [f"orch:{orch_id}"]):
        orch = store.get_orch(con, orch_id)
        if not revivable(orch):
            why = ("is not registered" if orch is None else "was stopped with `orchd orch stop`"
                   if orch["stopped_at"] is not None else "is not a Claude Orch with a session")
            raise ValueError(f"orch {orch_id} {why}; it is not revived")
        if seen_job is not None:
            if orch["job_id"] != seen_job:
                return orch
        health = owner_health(orch, rt.live_jobs())
        if health["state"] != "dead" or health["reason"] not in REVIVABLE:
            return orch
        # Spawn under the flock only, never inside a SQLite write transaction.
        sock, job, session = rt.start_orch(orch_id, orch["model"], paths.orch_home(), resume=orch["session_id"])
        with store.immediate(con):
            changed = con.execute(
                "UPDATE orchs SET socket=?, job_id=?, session_id=? "
                "WHERE id=? AND job_id IS ? AND session_id IS ? AND stopped_at IS NULL",
                (sock, job, session, orch_id, orch["job_id"], orch["session_id"])).rowcount
        if changed != 1:
            rt.stop_worker(job)
            raise RuntimeError(f"orch {orch_id} changed while reviving it; stopped the resumed job {job}")
        return store.get_orch(con, orch_id)


def send(con, rt, orch_id, text):
    """Send through verified Orch transport; only confirmed provider death may revive.

    Returns the row the text went to. Missing transport on a live owner is held.
    """
    orch = store.get_orch(con, orch_id)
    try:
        rt.send_orch(orch, text)
    except GONE:
        if not revivable(orch):
            raise
        health = owner_health(orch, rt.live_jobs())
        if health["state"] != "dead" or health["reason"] not in REVIVABLE:
            from .orch_transport import TransportUnavailable
            raise TransportUnavailable("Orch transport is unknown; a live owner is not revived") from None
        orch = revive(con, rt, orch_id, seen_job=orch["job_id"])
        rt.send_orch(orch, text)
    return orch
