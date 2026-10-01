"""Read-only Orch health from a runtime snapshot, independent of worker health.

Registry timestamps are bookkeeping, not liveness proof. Claude job membership
is the available probe; Codex threads have no equivalent probe here.
"""


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
            state, reason = "alive", "job_present"
        else:
            state, reason = "dead", "job_absent"
    return {"state": state, "reason": reason}
