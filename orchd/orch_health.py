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
            # `state` is the task outcome, not process liveness: per
            # https://code.claude.com/docs/en/agent-view#list-sessions-as-json pid/status appear only while the
            # process is alive, and the CLI mapper can emit state=failed with a live pid + status idle/waiting.
            # So failed is dead only when the entry carries no live-process evidence at all; evidence we cannot
            # read is unknown, never guessed. pids are not probed locally (reuse proves nothing about identity).
            entry = jobs[orch["job_id"]]
            if entry.get("state") != "failed":
                state, reason = "alive", "job_present"
            elif entry.get("pid") is None and entry.get("status") is None:
                state, reason = "dead", "job_failed"
            elif (isinstance(entry.get("pid"), int) and not isinstance(entry["pid"], bool)
                  and entry["pid"] > 0 and entry.get("status") in LIVE_STATUSES):
                state, reason = "alive", "job_present"
            else:
                state, reason = "unknown", "job_failed_unverified"
        else:
            state, reason = "dead", "job_absent"
    return {"state": state, "reason": reason}
