"""Operator-only, durable in-place Claude Orch MCP reload.

No author task or ownership records are changed. An uncertain spawn remains
quiesced until a fresh provider receipt identifies its exact original session.
"""
import argparse
import hashlib
import json
import math
import os
import re
import stat
import sys
import time
import uuid
from pathlib import Path

import psutil

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
    columns = {r["name"] for r in con.execute("PRAGMA table_info(orch_reload_operations)")}
    for column in ("preflight", "stop_evidence", "lineage", "recovery", "spawn_failure"):
        if column not in columns:
            con.execute(f"ALTER TABLE orch_reload_operations ADD COLUMN {column} TEXT")


def status(con, operation_id):
    _operator()
    _schema(con)
    row = con.execute("SELECT * FROM orch_reload_operations WHERE operation_id=?", (operation_id,)).fetchone()
    if row is None:
        raise ValueError("reload operation not found")
    result = dict(row)
    result["baseline"] = json.loads(result["baseline"])
    result["receipt"] = json.loads(result["receipt"]) if result["receipt"] else None
    for key in ("preflight", "stop_evidence", "lineage", "recovery", "spawn_failure"):
        result[key] = json.loads(result[key]) if result[key] else None
    return result


def _process_birth(pid):
    return psutil.Process(pid).create_time()


def _provider(rt):
    return {"command": str(Path(rt.claude).resolve()),
            "config": str(Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))).resolve())}


def _descriptor(row, item):
    if (not isinstance(item, dict) or item.get("id") != row["old_job"] or
            item.get("sessionId") != row["session_id"] or not item.get("cwd") or
            str(Path(item["cwd"]).resolve()) != row["home"] or
            not isinstance(item.get("pid"), int) or isinstance(item.get("pid"), bool) or item["pid"] <= 0 or
            not isinstance(item.get("startedAt"), (int, float)) or isinstance(item.get("startedAt"), bool) or
            not math.isfinite(item["startedAt"]) or item["startedAt"] <= 0):
        raise ValueError("preflight job descriptor does not identify the original process")


def attach_stop_preflight(con, rt, operation_id, file, sha256):
    """Attach an Operator-protected, pre-stop snapshot to an older pending operation.

    The Operator supplies its exact SHA; this imports evidence, never stops or
    spawns. Without a saved OS birth, only NoSuchProcess can prove that PID gone.
    """
    _operator()
    row = status(con, operation_id)
    with store.task_delivery(con, [f"orch:{row['orch_id']}"]):
        row = status(con, operation_id)
        _current(con, row)
        if row["phase"] != "stop_pending":
            raise ValueError("preflight attachment requires stop_pending")
        fd = os.open(file, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                    info.st_mode & 0o022 or info.st_mtime > row["created_at"]):
                raise ValueError("preflight must be an owned protected regular file saved before stop")
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024 or hashlib.sha256(raw).hexdigest() != sha256:
            raise ValueError("preflight SHA does not match")
        snapshot = json.loads(raw)
        old = snapshot["orch"]
        if any(old.get(k) != row[v] for k, v in (("id", "orch_id"), ("job_id", "old_job"),
                                                ("session_id", "session_id"), ("model", "model"))):
            raise ValueError("preflight Orch identity does not match operation")
        item = snapshot["job"]
        _descriptor(row, item)
        evidence = {"job": item, "provider": _provider(rt), "process_birth": None,
                    "source_sha256": sha256, "source_mtime": info.st_mtime,
                    "source": "operator_pre_stop_snapshot"}
        if row["preflight"] is not None and row["preflight"] != evidence:
            raise ValueError("preflight evidence is immutable once attached")
        with store.immediate(con):
            con.execute("UPDATE orch_reload_operations SET preflight=? WHERE operation_id=?",
                        (json.dumps(evidence), operation_id))
        return status(con, operation_id)


def _stop_proof(row, rt, jobs, listening):
    if listening is not False or not isinstance(jobs, dict) or any(
            not isinstance(k, str) or not isinstance(v, dict) for k, v in jobs.items()):
        return None
    saved = row["preflight"]
    if saved and saved["provider"] != _provider(rt):
        return None
    # A listed original session with any process evidence, or an unidentified
    # live job, prevents claiming that the provider has no original process.
    for item in jobs.values():
        retired = item.get("pid") is None and item.get("status") is None
        if not retired and (item.get("sessionId") == row["session_id"] or
                            (not isinstance(item.get("sessionId"), str) or not item["sessionId"].strip())):
            return None
    old = jobs.get(row["old_job"])
    if (isinstance(old, dict) and "pid" in old and "status" in old and
            old.get("id") == row["old_job"] and old.get("sessionId") == row["session_id"] and
            old["pid"] is None and old["status"] is None):
        return {"kind": "provider_retired_and_socket_closed"}
    if not saved:
        return None
    item = saved["job"]
    _descriptor(row, item)
    try:
        birth = _process_birth(item["pid"])
    except psutil.NoSuchProcess:
        return {"kind": "original_pid_absent", "pid": item["pid"],
                "provider_started_at": item["startedAt"], "checked_at": time.time()}
    except (psutil.AccessDenied, OSError, RuntimeError):
        return None
    original_birth = saved.get("process_birth")
    if original_birth is not None and birth != original_birth:
        return {"kind": "original_pid_reused", "pid": item["pid"], "original_birth": original_birth,
                "current_birth": birth, "checked_at": time.time()}
    return None


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
            descriptor_row = {"old_job": expected_job, "session_id": expected_session,
                              "home": str(paths.orch_home().resolve())}
            _descriptor(descriptor_row, item)
            birth = _process_birth(item["pid"])
            evidence = {"job": item, "provider": _provider(rt), "process_birth": birth,
                        "source": "native_pre_stop_probe"}
            marker = time.time()
            with store.immediate(con):
                con.execute("INSERT INTO orch_reload_operations "
                            "(operation_id,orch_id,session_id,old_job,model,home,marker,phase,baseline,"
                            "receipt,error,created_at,preflight) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (operation_id, orch_id, expected_session, expected_job, expected_model,
                             str(paths.orch_home().resolve()), marker, "stop_pending", json.dumps(list(jobs)),
                             None, None, marker, json.dumps(evidence)))
                changed = con.execute("UPDATE orchs SET stopped_at=? WHERE id=? AND stopped_at IS NULL "
                                      "AND job_id=? AND session_id=? AND model=?",
                                      (marker, orch_id, expected_job, expected_session, expected_model)).rowcount
                if changed != 1:
                    raise ValueError("original Orch changed before quiescing")
            row = status(con, operation_id)
        orch = _current(con, row)
        if row["preflight"] and row["preflight"]["provider"] != _provider(rt):
            raise ValueError("reload provider profile changed; no stop requested")
        try:
            rt.stop_worker(expected_job)
            jobs = rt.live_jobs()
            listening = rt.socket_listening(orch["socket"])
        except Exception:
            _update(con, operation_id, "stop_pending", error="old_stop_confirmation_unknown")
            return status(con, operation_id)
        proof = _stop_proof(row, rt, jobs, listening)
        if proof is None:
            _update(con, operation_id, "stop_pending", error="old_job_not_confirmed_stopped")
            return status(con, operation_id)
        _current(con, row)
        with store.immediate(con):
            con.execute("UPDATE orch_reload_operations SET stop_evidence=?,phase='spawn_pending',error=NULL "
                        "WHERE operation_id=?", (json.dumps(proof), operation_id))
        try:
            sock, job, session = rt.start_orch(orch_id, expected_model, Path(row["home"]), resume=expected_session)
        except Exception as error:
            # Exception text/argv can contain prompts, credentials and replies.
            # Retain only bounded structural diagnostics, never str(error).
            failure = {"type": type(error).__name__, "recorded_at": time.time()}
            stage = getattr(error, "launch_stage", None)
            if isinstance(stage, str) and stage == "native_no_job_receipt":
                failure["launch_stage"] = stage
            for key in ("errno", "returncode", "timeout"):
                value = getattr(error, key, None)
                if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
                    failure[key] = value
            for key in ("stdout", "stderr"):
                value = getattr(error, key, None)
                if isinstance(value, str):
                    value = value.encode("utf-8", errors="replace")
                if isinstance(value, bytes):
                    failure[key + "_sha256"] = hashlib.sha256(value).hexdigest()
                    failure[key + "_bytes"] = len(value)
            with store.immediate(con):
                con.execute("UPDATE orch_reload_operations SET spawn_failure=? WHERE operation_id=?",
                            (json.dumps(failure), operation_id))
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


def _protected_json(file, sha256, *, private=False, newer_than=None):
    fd = os.open(file, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022 or
                (private and stat.S_IMODE(info.st_mode) != 0o600)):
            raise ValueError("provider evidence must be an owned protected regular file")
        if newer_than is not None and info.st_mtime < newer_than:
            raise ValueError("provider evidence was saved before this recovery launch finished")
        raw = stream.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024 or hashlib.sha256(raw).hexdigest() != sha256:
        raise ValueError("provider evidence SHA does not match")
    return json.loads(raw)


def _flag(flags, name):
    if not isinstance(flags, list) or not all(isinstance(f, str) for f in flags) or flags.count(name) != 1:
        raise ValueError("provider launch flags do not identify the original configuration")
    index = flags.index(name)
    if index + 1 >= len(flags):
        raise ValueError("provider launch flag has no value")
    return flags[index + 1]


def _process_parent(pid):
    return psutil.Process(pid).ppid()


def _copy_proof(con, rt, row, orch, receipt, roster, sha256):
    """Full lineage/MCP/process proof, shared by attachment and final acceptance."""
    if not isinstance(roster, dict) or not isinstance(roster.get("workers"), dict):
        raise ValueError("provider roster workers are malformed")
    workers = roster["workers"]
    # Native acceptance can precede publication in live_jobs. Never treat a
    # missing public job as evidence that an accepted competitor is absent.
    for job, candidate in workers.items():
        if not isinstance(job, str) or not isinstance(candidate, dict):
            raise ValueError("provider roster worker is malformed")
        candidate_dispatch = candidate.get("dispatch", {})
        if not isinstance(candidate_dispatch, dict) or not isinstance(candidate_dispatch.get("launch", {}), dict):
            raise ValueError("provider roster launch is malformed")
        session = candidate.get("sessionId")
        if not isinstance(session, str) or not session.strip():
            raise ValueError("provider roster worker session is malformed")
        if "sessionId" in candidate_dispatch:
            dispatch_session = candidate_dispatch["sessionId"]
            if (not isinstance(dispatch_session, str) or not dispatch_session.strip() or
                    dispatch_session != session):
                raise ValueError("provider roster dispatch session is malformed or ambiguous")
        cwd = candidate.get("cwd")
        if not isinstance(cwd, str) or not cwd.strip() or not Path(cwd).is_absolute():
            raise ValueError("provider roster worker scope is malformed")
        canonical_cwd = str(Path(cwd).resolve())
        if "cwd" in candidate_dispatch:
            dispatch_cwd = candidate_dispatch["cwd"]
            if (not isinstance(dispatch_cwd, str) or not dispatch_cwd.strip() or
                    not Path(dispatch_cwd).is_absolute() or str(Path(dispatch_cwd).resolve()) != canonical_cwd):
                raise ValueError("provider roster dispatch scope is malformed or ambiguous")
        parent = candidate_dispatch.get("launch", {})
        same_parent = parent.get("mode") == "resume" and parent.get("sessionId") == row["session_id"]
        same_home = canonical_cwd == row["home"]
        if job != receipt["job_id"] and (session in (row["session_id"], receipt["session_id"]) or
                same_parent or (job not in row["baseline"] and same_home)):
            raise ValueError("ambiguous accepted controller or original-session resume copy")
    worker = workers.get(receipt["job_id"], {})
    dispatch = worker.get("dispatch", {})
    launch = dispatch.get("launch", {})
    if (roster.get("proto") != 1 or dispatch.get("proto") != 1 or
            worker.get("sessionId") != receipt["session_id"] or dispatch.get("sessionId") != receipt["session_id"] or
            dispatch.get("short") != receipt["job_id"] or worker.get("cwd") != row["home"] or
            dispatch.get("cwd") != row["home"] or launch.get("mode") != "resume" or
            launch.get("sessionId") != row["session_id"] or launch.get("fork") is not True or
            launch.get("restoresTranscript") is not True or
            not isinstance(dispatch.get("createdAt"), (int, float)) or
            isinstance(dispatch.get("createdAt"), bool) or
            not math.isfinite(dispatch["createdAt"]) or dispatch["createdAt"] < row["created_at"] * 1000):
        raise ValueError("provider roster does not prove this copy's exact original-session lineage")
    flags = launch.get("flagArgs")
    mcp_path = str(Path(orch["socket"]).parent / "mcp.json")
    if (_flag(flags, "--model") != row["model"] or _flag(flags, "--mcp-config") != mcp_path or
            _flag(flags, "--messaging-socket-path") != orch["socket"] or
            "--strict-mcp-config" not in flags or receipt.get("socket") != orch["socket"]):
        raise ValueError("copy launch does not preserve the original model/socket/MCP configuration")
    fd = os.open(mcp_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise ValueError("MCP configuration is not protected")
        mcp_raw = stream.read(1024 * 1024 + 1)
    if len(mcp_raw) > 1024 * 1024:
        raise ValueError("MCP configuration exceeds size limit")
    mcp = json.loads(mcp_raw).get("mcpServers", {}).get("orchd", {})
    entrypoint = paths.orchd_executable()
    executable, args = paths.mcp_launch(entrypoint) if os.name == "nt" else (sys.executable, [entrypoint])
    from .runtime import mcp_env
    if (mcp.get("command") != executable or mcp.get("args") != [*args, "mcp"] or
            mcp.get("env") != mcp_env(row["orch_id"])):
        raise ValueError("copy MCP does not use this deployment and original Orch identity/profile")
    database = Path(con.execute("PRAGMA database_list").fetchone()[2]).resolve()
    if database != (Path(store.home()) / "orchd.db").resolve():
        raise ValueError("copy MCP profile does not address the operation database")
    jobs = rt.live_jobs()
    copy_row = dict(row, session_id=receipt["session_id"])
    if not _verified(copy_row, jobs, receipt["job_id"]) or rt.socket_listening(orch["socket"]) is not True:
        raise ValueError("copy is not positively alive on the original socket")
    for job, item in jobs.items():
        if not isinstance(item, dict):
            raise ValueError("provider job evidence is malformed")
        active = item.get("pid") is not None or item.get("status") is not None
        session = item.get("sessionId")
        if active and (not isinstance(session, str) or not session.strip() or
                       not isinstance(item.get("cwd"), str) or not item["cwd"].strip() or
                       not Path(item["cwd"]).is_absolute() or session == row["session_id"] or (session == receipt["session_id"] and job != receipt["job_id"])):
            raise ValueError("original or copied session has ambiguous competing process evidence")
        worker_copy = roster.get("workers", {}).get(job, {})
        parent = worker_copy.get("dispatch", {}).get("launch", {})
        related_copy = parent.get("mode") == "resume" and parent.get("sessionId") == row["session_id"]
        if active and job != receipt["job_id"] and (related_copy or
                (item.get("cwd") and str(Path(item["cwd"]).resolve()) == row["home"] and
                 job not in row["baseline"])):
            raise ValueError("ambiguous competing controller or original-session resume copy")
    pid = jobs[receipt["job_id"]]["pid"]
    host = worker.get("pid")
    if not isinstance(host, int) or isinstance(host, bool) or host <= 0 or (host != pid and _process_parent(pid) != host):
        raise ValueError("fresh copy PID does not belong to the provider roster host")
    return {"kind": "operator_accepted_provider_resume_copy", "original_session": row["session_id"],
               "accepted_session": receipt["session_id"], "job_id": receipt["job_id"], "pid": pid,
               "provider_host_pid": host, "roster_sha256": sha256,
               "mcp_sha256": hashlib.sha256(mcp_raw).hexdigest(), "provider": _provider(rt),
               "verified_at": time.time()}


def accept_resume_copy(con, rt, operation_id, expected_job, expected_session, file, sha256):
    """Explicitly recover only an already running, provider-proven controller copy.

    This maintenance exception changes the controller's native session, never
    logical Orch ownership or any author task/session. It cannot launch or stop.
    """
    _operator()
    row = status(con, operation_id)
    with store.task_delivery(con, [f"orch:{row['orch_id']}"]):
        row = status(con, operation_id)
        receipt = row["receipt"]
        if (not isinstance(receipt, dict) or receipt.get("job_id") != expected_job or
                receipt.get("session_id") != expected_session or expected_session == row["session_id"] or
                expected_job in row["baseline"]):
            raise ValueError("copy must exactly match this operation's returned job and session")
        if row["phase"] == "complete" and row["lineage"]:
            orch = store.get_orch(con, row["orch_id"])
            if (row["lineage"]["roster_sha256"] != sha256 or orch is None or
                    (orch["session_id"], orch["job_id"], orch["model"], orch["stopped_at"]) !=
                    (expected_session, expected_job, row["model"], None)):
                raise ValueError("accepted copy identity or evidence changed")
            return row
        orch = _current(con, row)
        if row["phase"] != "held" or row["error"] != "returned_session_mismatch" or not row["stop_evidence"]:
            raise ValueError("only a proven-stopped reload held for a returned session mismatch can accept a copy")
        if not row["preflight"] or row["preflight"]["provider"] != _provider(rt):
            raise ValueError("reload provider profile changed")
        roster = _protected_json(file, sha256)
        lineage = _copy_proof(con, rt, row, orch, receipt, roster, sha256)
        _current(con, row)
        with store.immediate(con):
            changed = con.execute("UPDATE orchs SET session_id=?,job_id=?,socket=?,stopped_at=NULL "
                                  "WHERE id=? AND session_id=? AND job_id=? AND model=? AND stopped_at=?",
                                  (expected_session, expected_job, receipt["socket"], row["orch_id"],
                                   row["session_id"], row["old_job"], row["model"], row["marker"])).rowcount
            if changed != 1:
                raise ValueError("controller changed before copy acceptance; no ownership moved")
            con.execute("UPDATE orch_reload_operations SET phase='complete',error=NULL,lineage=? WHERE operation_id=?",
                        (json.dumps(lineage), operation_id))
        return status(con, operation_id)


def attach_recovery_receipt(con, rt, operation_id, receipt_file, receipt_sha256, roster_file, roster_sha256):
    """Attach an actual protected recovery launch receipt; never launch or accept.

    The trusted local Operator supplies these artifacts. Digests bind bytes,
    not authorship: provider lineage and fresh process proof remain mandatory.
    """
    _operator()
    row = status(con, operation_id)
    with store.task_delivery(con, [f"orch:{row['orch_id']}"]):
        row = status(con, operation_id)
        evidence = _protected_json(receipt_file, receipt_sha256, private=True)
        if not isinstance(evidence, dict) or set(evidence) != {
                "version", "operation_id", "original", "launcher", "started_at", "finished_at",
                "exit_code", "native_stdout", "receipt"}:
            raise ValueError("invalid recovery receipt schema")
        original = {"orch_id": row["orch_id"], "session_id": row["session_id"], "job_id": row["old_job"],
                    "model": row["model"], "home": row["home"]}
        orch = store.get_orch(con, row["orch_id"])
        if orch is None:
            raise ValueError("original Orch is missing")
        original["socket"] = orch["socket"]
        receipt, launcher = evidence["receipt"], evidence["launcher"]
        if (type(evidence["version"]) is not int or evidence["version"] != 1 or
                evidence["operation_id"] != operation_id or evidence["original"] != original or
                type(evidence["exit_code"]) is not int or evidence["exit_code"] != 0 or
                not isinstance(receipt, dict) or set(receipt) != {"job_id", "session_id", "socket"} or
                not all(isinstance(v, str) and v for v in receipt.values()) or
                receipt["socket"] != orch["socket"] or receipt["job_id"] in row["baseline"] or
                receipt["session_id"] == row["session_id"] or
                evidence["native_stdout"] != "claude attach " + receipt["job_id"] or
                not re.fullmatch(r"[a-zA-Z0-9-]+", receipt["job_id"])):
            raise ValueError("recovery receipt does not bind the exact operation and returned copy")
        if (not isinstance(launcher, dict) or set(launcher) != {"command", "config"} or
                not all(isinstance(v, str) and v for v in launcher.values()) or
                not Path(launcher["command"]).is_absolute() or
                str(Path(launcher["command"]).resolve()) != launcher["command"] or
                not Path(launcher["command"]).is_file() or
                not os.access(launcher["command"], os.X_OK) or not row["preflight"] or
                launcher["config"] != row["preflight"]["provider"]["config"] or
                row["preflight"]["provider"] != _provider(rt)):
            raise ValueError("recovery launcher or original provider reader/profile is invalid")
        start, finish = evidence["started_at"], evidence["finished_at"]
        if (any(not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v)
                for v in (start, finish)) or not row["created_at"] <= start <= finish <= time.time() or
                not row["stop_evidence"]):
            raise ValueError("recovery launch time or original stop evidence is invalid")
        roster = _protected_json(roster_file, roster_sha256, private=True, newer_than=finish)
        if not isinstance(roster, dict) or not isinstance(roster.get("workers"), dict):
            raise ValueError("provider roster workers are malformed")
        worker = roster["workers"].get(receipt["job_id"], {})
        if not isinstance(worker, dict) or not isinstance(worker.get("dispatch"), dict):
            raise ValueError("provider copy dispatch is malformed")
        dispatch = worker["dispatch"]
        created = dispatch.get("createdAt")
        if (not isinstance(created, (int, float)) or isinstance(created, bool) or
                not math.isfinite(created) or not start * 1000 <= created <= finish * 1000):
            raise ValueError("provider copy timestamp is outside the actual launch interval")
        recovery = {"receipt_sha256": receipt_sha256, "roster_sha256": roster_sha256,
                    "launcher": launcher, "started_at": start, "finished_at": finish,
                    "previous_error": row["error"] if row["recovery"] is None else row["recovery"]["previous_error"]}
        if row["recovery"] is not None:
            if row["recovery"] != recovery or row["receipt"] != receipt:
                raise ValueError("recovery evidence is immutable once attached")
            if row["phase"] == "complete":
                if (orch["session_id"], orch["job_id"], orch["model"], orch["stopped_at"]) != (
                        receipt["session_id"], receipt["job_id"], row["model"], None):
                    raise ValueError("accepted recovery controller changed")
            else:
                _current(con, row)
        else:
            _current(con, row)
            if row["phase"] != "spawn_pending" or row["receipt"] is not None:
                raise ValueError("recovery attachment requires an uncertain spawn with no receipt")
        _copy_proof(con, rt, row, orch, receipt, roster, roster_sha256)
        if row["recovery"] is not None:
            return row
        with store.immediate(con):
            _current(con, row)
            changed = con.execute("UPDATE orch_reload_operations SET phase='held',error='returned_session_mismatch',"
                "receipt=?,recovery=? WHERE operation_id=? AND phase='spawn_pending' AND receipt IS NULL "
                "AND recovery IS NULL", (json.dumps(receipt), json.dumps(recovery), operation_id)).rowcount
            if changed != 1:
                raise ValueError("reload operation changed before recovery attachment")
        return status(con, operation_id)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("reload", "status", "reconcile", "attach-stop-preflight", "accept-resume-copy", "attach-recovery-receipt"))
    parser.add_argument("--state", required=True)
    parser.add_argument("--operation", required=True)
    parser.add_argument("--orch")
    parser.add_argument("--expected-session")
    parser.add_argument("--expected-job")
    parser.add_argument("--expected-model")
    parser.add_argument("--candidate-job")
    parser.add_argument("--candidate-session")
    parser.add_argument("--roster-file")
    parser.add_argument("--roster-sha256")
    parser.add_argument("--preflight-file")
    parser.add_argument("--preflight-sha256")
    parser.add_argument("--receipt-file")
    parser.add_argument("--receipt-sha256")
    args = parser.parse_args(argv)
    _operator()
    from .runtime import Runtime
    con = store.connect(args.state)
    try:
        if args.command == "reload":
            result = reload(con, Runtime(), args.orch, args.operation, args.expected_session,
                            args.expected_job, args.expected_model)
        elif args.command == "attach-stop-preflight":
            result = attach_stop_preflight(con, Runtime(), args.operation, args.preflight_file,
                                           args.preflight_sha256)
        elif args.command == "accept-resume-copy":
            result = accept_resume_copy(con, Runtime(), args.operation, args.candidate_job,
                                        args.candidate_session, args.roster_file, args.roster_sha256)
        elif args.command == "attach-recovery-receipt":
            result = attach_recovery_receipt(con, Runtime(), args.operation, args.receipt_file,
                args.receipt_sha256, args.roster_file, args.roster_sha256)
        elif args.command == "reconcile":
            result = reconcile(con, Runtime(), args.operation, args.candidate_job)
        else:
            result = status(con, args.operation)
        print(json.dumps(result, sort_keys=True))
        return 0 if result["phase"] == "complete" or args.command in ("status", "attach-stop-preflight") else 2
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
