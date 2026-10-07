"""Read-only Orch health from a runtime snapshot, independent of worker health.

Registry timestamps are bookkeeping, not liveness proof. Claude job membership
is the available probe; Codex threads have no equivalent probe here.
"""

LIVE_STATUSES = ("idle", "waiting", "busy")


def owner_health(orch, jobs):
    state, reason = "unknown", "owner_unregistered"
    if orch is not None:
        if orch["kind"] == "codex":
            reason = "codex_unverified"
        elif orch["kind"] != "claude":
            reason = "kind_unsupported"
        elif not orch["job_id"]:
            reason = "job_missing"
        elif jobs is None:
            reason = "runtime_unavailable"
        elif not isinstance(jobs, dict) or any(
            not isinstance(k, str) or not k or not isinstance(v, dict) for k, v in jobs.items()
        ):
            reason = "runtime_invalid"
        elif orch["job_id"] in jobs:
            # agents retains historical jobs. Only pid + a known live status is
            # process evidence; task outcome alone never proves liveness.
            entry = jobs[orch["job_id"]]
            if (isinstance(entry.get("pid"), int) and not isinstance(entry["pid"], bool)
                    and entry["pid"] > 0 and entry.get("status") in LIVE_STATUSES):
                state, reason = "alive", "job_present"
            elif entry.get("pid") is None and entry.get("status") is None:
                state, reason = "dead", "job_failed" if entry.get("state") == "failed" else "job_retired"
            else:
                reason = "job_failed_unverified" if entry.get("state") == "failed" else "job_unverified"
        else:
            state, reason = "dead", "job_absent"
    return {"state": state, "reason": reason}


def session_health(orch, jobs, rt):
    """Registry/session probe shared by inventory and foreground selection.

    A resumable conversation is usable even after its daemon process retires.
    Socket errors and contradictory session identities are unknown, not death.
    """
    health = owner_health(orch, jobs)
    if orch is None or orch["kind"] != "claude" or health["state"] == "unknown":
        return health
    entry = jobs.get(orch["job_id"], {})
    if entry.get("sessionId") and orch["session_id"] and entry["sessionId"] != orch["session_id"]:
        return {"state": "unknown", "reason": "session_mismatch"}
    if orch["socket"]:
        try:
            listening = rt.socket_listening(orch["socket"])
        except Exception:
            return {"state": "unknown", "reason": "socket_unavailable"}
        if listening is not True and listening is not False:
            return {"state": "unknown", "reason": "socket_invalid"}
        if listening:
            if health["state"] == "dead":
                return {"state": "unknown", "reason": "socket_job_conflict"}
            return health
        if health["state"] == "alive":
            if orch["stopped_at"] is not None or not orch["session_id"]:
                return {"state": "unknown", "reason": "socket_job_conflict"}
            health = {"state": "dead", "reason": "job_retired"}
    if (health["state"] == "dead" and orch["stopped_at"] is None
            and orch["session_id"] and orch["job_id"] and orch["socket"]):
        return {"state": "idle", "reason": "session_resumable"}
    return health
