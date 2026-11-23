"""Operator-only, durable in-place Claude Orch MCP reload.

No author task or ownership records are changed. An uncertain spawn remains
quiesced until a fresh provider receipt identifies its exact original session.
"""
import argparse
import json
import os
import time
import uuid
from pathlib import Path

from . import paths, store
from .orch_health import owner_health


def _operator():
    if any(os.environ.get(k) for k in ("ORCHD_TASK_ID", "ORCHD_ORCH_ID", "ORCHD_WORKTREE")):
        raise PermissionError("Orch reload is an Operator-only entry point")


def _schema(con):
    con.execute("""CREATE TABLE IF NOT EXISTS orch_reload_operations (
        operation_id TEXT PRIMARY KEY, orch_id TEXT NOT NULL, session_id TEXT NOT NULL,
        old_job TEXT NOT NULL, model TEXT NOT NULL, home TEXT NOT NULL,
        marker REAL NOT NULL, phase TEXT NOT NULL, baseline TEXT NOT NULL,
        receipt TEXT, error TEXT, created_at REAL NOT NULL)""")


def status(con, operation_id):
    _operator()
    _schema(con)
    row = con.execute("SELECT * FROM orch_reload_operations WHERE operation_id=?", (operation_id,)).fetchone()
    if row is None:
        raise ValueError("reload operation not found")
    result = dict(row)
    result["baseline"] = json.loads(result["baseline"])
    result["receipt"] = json.loads(result["receipt"]) if result["receipt"] else None
    return result


def _update(con, op, phase, *, error=None, receipt=None):
    with store.immediate(con):
        con.execute("UPDATE orch_reload_operations SET phase=?,error=?,receipt=COALESCE(?,receipt) "
                    "WHERE operation_id=?", (phase, error, json.dumps(receipt) if receipt else None, op))


def _current(con, row):
    orch = store.get_orch(con, row["orch_id"])
    if orch is None or any(orch[k] != row[v] for k, v in
                           (("session_id", "session_id"), ("job_id", "old_job"), ("model", "model"),
                            ("stopped_at", "marker"))) or orch["kind"] != "claude":
        raise ValueError("Orch changed during reload; keep operation held and inspect")
    return orch


def _verified(row, jobs, job):
    if not isinstance(jobs, dict) or job not in jobs or job in row["baseline"]:
        return False
    item = jobs[job]
    if not isinstance(item, dict) or item.get("id", job) != job:
        return False
    if item.get("sessionId") != row["session_id"] or not item.get("cwd"):
        return False
    if str(Path(item["cwd"]).resolve()) != row["home"]:
        return False
    return owner_health({"kind": "claude", "job_id": job}, jobs)["state"] == "alive"


def _finish(con, rt, row, receipt):
    if not all(isinstance(receipt.get(k), str) and receipt[k] for k in ("socket", "job_id", "session_id")):
        _update(con, row["operation_id"], "held", error="invalid_spawn_receipt", receipt=receipt)
        return status(con, row["operation_id"])
    if receipt["session_id"] != row["session_id"]:
        _update(con, row["operation_id"], "held", error="returned_session_mismatch", receipt=receipt)
        # A mismatched provider receipt is not proof that this operation owns
        # that job. Even a job absent from the baseline can be a concurrently
        # started author; preserve it and require positive reconciliation.
        return status(con, row["operation_id"])
    try:
        jobs = rt.live_jobs()
        verified = (_verified(row, jobs, receipt["job_id"]) and
                    rt.socket_listening(receipt["socket"]) is True)
    except Exception:
        _update(con, row["operation_id"], "receipt", error="new_job_probe_unknown", receipt=receipt)
        return status(con, row["operation_id"])
    if not verified:
        _update(con, row["operation_id"], "receipt", error="new_job_not_verified", receipt=receipt)
        return status(con, row["operation_id"])
    _current(con, row)
    with store.immediate(con):
        changed = con.execute("UPDATE orchs SET socket=?,job_id=?,stopped_at=NULL "
                              "WHERE id=? AND session_id=? AND job_id=? AND model=? AND stopped_at=?",
                              (receipt["socket"], receipt["job_id"], row["orch_id"], row["session_id"],
                               row["old_job"], row["model"], row["marker"])).rowcount
        if changed != 1:
            raise ValueError("Orch changed during reload; replacement remains held")
        con.execute("UPDATE orch_reload_operations SET phase='complete',error=NULL,receipt=? WHERE operation_id=?",
                    (json.dumps(receipt), row["operation_id"]))
    return status(con, row["operation_id"])


def reload(con, rt, orch_id, operation_id, expected_session, expected_job, expected_model):
    """Stop and resume only the exact original idle Orch; repeated op IDs never respawn.

    Run this module under the new deployment's interpreter and profile env so
    Runtime.start_orch writes MCP config using that interpreter/entry point.
    """
    _operator()
    if str(uuid.UUID(operation_id)) != operation_id:
        raise ValueError("operation_id must be a canonical UUID")
    if not all(isinstance(v, str) and v for v in (orch_id, expected_session, expected_job, expected_model)):
        raise ValueError("exact Orch/session/job/model required")
    _schema(con)
    with store.task_delivery(con, [f"orch:{orch_id}"]):
        prior = con.execute("SELECT * FROM orch_reload_operations WHERE operation_id=?", (operation_id,)).fetchone()
        if prior:
            if tuple(prior[k] for k in ("orch_id", "session_id", "old_job", "model")) != (
                    orch_id, expected_session, expected_job, expected_model):
                raise ValueError("operation_id already belongs to another reload identity")
            row = status(con, operation_id)
            if row["phase"] != "stop_pending":
                return row
        else:
            orch = store.get_orch(con, orch_id)
            if (orch is None or orch["kind"] != "claude" or orch["stopped_at"] is not None or
                    (orch["session_id"], orch["job_id"], orch["model"]) !=
                    (expected_session, expected_job, expected_model)):
                raise ValueError("original Orch identity does not match, or is stopped")
            jobs = rt.live_jobs()
            item = jobs.get(expected_job, {}) if isinstance(jobs, dict) else {}
            if (owner_health(orch, jobs)["state"] != "alive" or item.get("status") != "idle" or
                    item.get("state") != "blocked" or item.get("sessionId") != expected_session or
                    not item.get("cwd") or Path(item["cwd"]).resolve() != paths.orch_home().resolve()):
                raise ValueError("original Orch is not positively identified and idle/blocked")
            marker = time.time()
            with store.immediate(con):
                con.execute("INSERT INTO orch_reload_operations VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                            (operation_id, orch_id, expected_session, expected_job, expected_model,
                             str(paths.orch_home().resolve()), marker, "stop_pending", json.dumps(list(jobs)),
                             None, None, marker))
                changed = con.execute("UPDATE orchs SET stopped_at=? WHERE id=? AND stopped_at IS NULL "
                                      "AND job_id=? AND session_id=? AND model=?",
                                      (marker, orch_id, expected_job, expected_session, expected_model)).rowcount
                if changed != 1:
                    raise ValueError("original Orch changed before quiescing")
            row = status(con, operation_id)
        orch = _current(con, row)
        try:
            rt.stop_worker(expected_job)
            jobs = rt.live_jobs()
            listening = rt.socket_listening(orch["socket"])
        except Exception:
            _update(con, operation_id, "stop_pending", error="old_stop_confirmation_unknown")
            return status(con, operation_id)
        # An absent job is insufficient evidence after a stop request.
        old = jobs.get(expected_job) if isinstance(jobs, dict) else None
        if (not isinstance(old, dict) or old.get("pid") is not None or old.get("status") is not None or
                listening is not False):
            _update(con, operation_id, "stop_pending", error="old_job_not_confirmed_stopped")
            return status(con, operation_id)
        _current(con, row)
        _update(con, operation_id, "spawn_pending")
        try:
            sock, job, session = rt.start_orch(orch_id, expected_model, Path(row["home"]), resume=expected_session)
        except Exception:
            _update(con, operation_id, "spawn_pending", error="spawn_acceptance_unknown")
            return status(con, operation_id)
        receipt = dict(socket=sock, job_id=job, session_id=session)
        _update(con, operation_id, "receipt", receipt=receipt)
        return _finish(con, rt, row, receipt)


def reconcile(con, rt, operation_id, candidate_job=None):
    """Verify a persisted receipt or explicit new job using fresh native evidence; never spawn.

    Without a persisted receipt, candidate_job must be supplied by the Operator
    from the provider's spawn receipt. Absence never clears an uncertain spawn.
    """
    _operator()
    row = status(con, operation_id)
    with store.task_delivery(con, [f"orch:{row['orch_id']}"]):
        row = status(con, operation_id)
        if row["phase"] == "complete":
            return row
        if row["phase"] not in ("spawn_pending", "receipt"):
            return row
        _current(con, row)
        receipt = row["receipt"]
        if receipt is None:
            jobs = rt.live_jobs()
            if not candidate_job or not _verified(row, jobs, candidate_job):
                return row
            # Reject ambiguous concurrent resumes of this conversation.
            matches = [j for j in jobs if _verified(row, jobs, j)]
            if matches != [candidate_job]:
                raise ValueError("ambiguous resumed session; keep reload held")
            receipt = dict(socket=rt.orch_socket_path(row["orch_id"]), job_id=candidate_job,
                           session_id=row["session_id"])
            _update(con, operation_id, "receipt", receipt=receipt)
        return _finish(con, rt, row, receipt)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("reload", "status", "reconcile"))
    parser.add_argument("--state", required=True)
    parser.add_argument("--operation", required=True)
    parser.add_argument("--orch")
    parser.add_argument("--expected-session")
    parser.add_argument("--expected-job")
    parser.add_argument("--expected-model")
    parser.add_argument("--candidate-job")
    args = parser.parse_args(argv)
    _operator()
    from .runtime import Runtime
    con = store.connect(args.state)
    try:
        if args.command == "reload":
            result = reload(con, Runtime(), args.orch, args.operation, args.expected_session,
                            args.expected_job, args.expected_model)
        elif args.command == "reconcile":
            result = reconcile(con, Runtime(), args.operation, args.candidate_job)
        else:
            result = status(con, args.operation)
        print(json.dumps(result, sort_keys=True))
        return 0 if result["phase"] == "complete" or args.command == "status" else 2
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
