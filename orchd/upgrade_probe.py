"""Require the replacement Orch to exercise its tools before completing an upgrade."""
import json
import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from . import entry_ops, paths, store
from .file_lock import try_lock
from .orch_health import owner_health
from .upgrade import _atomic_json


def _path(op_id, suffix):
    return entry_ops._record_path(op_id).with_suffix(suffix)


@contextmanager
def _lock(op_id):
    with _path(op_id, ".probe-lock").open("a+") as handle:
        try_lock(handle)
        yield


def dispatch_fields():
    from .mcp_server import TOOLS
    return sorted(next(t for t in TOOLS if t["name"] == "dispatch")["inputSchema"]["properties"])


def require_operator():
    if any(os.environ.get(k) for k in ("ORCHD_ORCH_ID", "ORCHD_TASK_ID", "ORCHD_WORKTREE")):
        raise PermissionError("Session verification is an Operator-only maintenance action")


def _live_controller(rt, orch):
    try:
        jobs = rt.live_jobs()
        if owner_health(orch, jobs)["state"] != "alive":
            return None
        job = jobs[orch["job_id"]]
        if (job.get("id") != orch["job_id"] or job.get("sessionId") != orch["session_id"] or not job.get("cwd")
                or Path(job["cwd"]).resolve() != paths.orch_home().resolve()
                or rt.socket_listening(orch["socket"]) is not True):
            return None
        return dict(job_id=orch["job_id"], session_id=orch["session_id"], pid=job["pid"],
                    cwd=str(paths.orch_home().resolve()), socket=orch["socket"])
    except (OSError, ValueError, RuntimeError, KeyError, TypeError):
        return None


def verify_current(con, rt, op_id, orch_id, expected_session, expected_job, **options):
    """Verify an exact already-refreshed controller without installing or changing ownership."""
    require_operator()
    path = entry_ops._record_path(op_id)
    with entry_ops._lock_file() as handle:
        try_lock(handle)
        orch = store.get_orch(con, orch_id)
        if (orch is None or orch["kind"] != "claude" or orch["stopped_at"] is not None
                or (orch["session_id"], orch["job_id"]) != (expected_session, expected_job)):
            raise ValueError("Exact controller identity does not match; no lifecycle action was taken")
        expected = dict(orch_id=orch_id, session_id=expected_session, job_id=expected_job)
        if path.exists():
            record = entry_ops.read(op_id)
            if record["kind"] != "session_tools" or record.get("expected_controller") != expected:
                raise ValueError("Operation ID already belongs to a different controller or operation")
        else:
            entry_ops._write(dict(op_id=op_id, kind="session_tools", restart=False,
                                  state="needs_session_refresh", session_refresh="pending",
                                  new_orch=orch_id, expected_controller=expected, restart_state="done",
                                  requested_at=time.time(), started_at=time.time(), finished_at=None,
                                  steps=[], summary="Controller tools have not been verified."))
    return verify(con, rt, op_id, **options)


def acknowledge(con, orch_id, op_id, challenge, fields):
    """Only the intended Claude controller may acknowledge its observed dispatch fields."""
    if os.environ.get("ORCHD_WORKTREE") or os.environ.get("ORCHD_TASK_ID"):
        raise PermissionError("Workers cannot verify an Orch upgrade")
    if not orch_id or os.environ.get("ORCHD_ORCH_ID") != orch_id:
        raise PermissionError("Upgrade verification requires the registered Orch MCP identity")
    with _lock(op_id):
        request = json.loads(_path(op_id, ".probe.json").read_text(encoding="utf-8"))
        record = entry_ops.read(op_id)
        orch = store.get_orch(con, orch_id)
        if (record["kind"] not in ("upgrade", "session_tools")
                or record["state"] not in ("running", "needs_session_refresh", "done", "done_with_warnings")
                or record.get("new_orch") != orch_id or request["orch_id"] != orch_id
                or orch is None or orch["kind"] != "claude" or orch["stopped_at"] is not None
                or (orch["session_id"], orch["job_id"], orch["socket"]) != (
                    request["session_id"], request["job_id"], request["socket"])
                or challenge != request["challenge"]):
            raise ValueError("Upgrade probe belongs to a different operation or controller")
        if (not isinstance(fields, list) or not all(isinstance(f, str) for f in fields)
                or sorted(fields) != request["dispatch_fields"] or sorted(fields) != dispatch_fields()):
            raise ValueError("Observed dispatch schema differs from the installed schema; refresh the session")
        path = _path(op_id, ".probe-ack.json")
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        receipt = dict(state="verified", op_id=op_id, orch_id=orch_id,
                       session_id=orch["session_id"], challenge=challenge,
                       dispatch_fields=sorted(fields), verified_at=time.time())
        _atomic_json(path, receipt)
        return receipt


def verify(con, rt, op_id, *, timeout=60, sleep=time.sleep, clock=time.monotonic):
    """Send one local probe; retries wait for the same receipt and never restart or dispatch."""
    require_operator()
    message = None
    with _lock(op_id):
        record = entry_ops.read(op_id)
        if (record["kind"] not in ("upgrade", "session_tools")
                or record["state"] not in ("running", "needs_session_refresh", "done", "done_with_warnings")):
            raise ValueError("Only an upgrade or controller-tools operation may verify session tools")
        orch = store.get_orch(con, record.get("new_orch"))
        if orch is None or orch["kind"] != "claude" or orch["stopped_at"] is not None:
            raise ValueError("Upgrade has no active replacement Claude Orch")
        path = _path(op_id, ".probe.json")
        if path.exists():
            request = json.loads(path.read_text(encoding="utf-8"))
            if (request["orch_id"], request["session_id"], request["job_id"], request["socket"],
                    request["dispatch_fields"]) != (
                    orch["id"], orch["session_id"], orch["job_id"], orch["socket"], dispatch_fields()):
                raise ValueError("Upgrade controller or schema changed; do not reuse the probe")
        else:
            request = dict(orch_id=orch["id"], session_id=orch["session_id"], job_id=orch["job_id"], socket=orch["socket"],
                           challenge=uuid.uuid4().hex, dispatch_fields=dispatch_fields())
            # Persist before transport: uncertain delivery must not send another prompt.
            _atomic_json(path, request)
            message = ("[orchd upgrade verification] Load the dispatch tool and inspect its actual input schema. "
                       "Call upgrade_probe with op_id=" + op_id + ", challenge=" + request["challenge"] +
                       ", and dispatch_fields containing every property name you actually see on dispatch. "
                       "Do not copy names from history or guess missing fields. This is a local tools check only; "
                       "do not dispatch, retry, adopt, message anyone, or change product/domain data. "
                       "If a tool or field is missing, report the gap in this session and remain idle.")
    # Do not hold the probe lock across delivery: the MCP response writes its receipt under that lock.
    if message is not None:
        try:
            rt.send_uds(orch["socket"], orch["session_id"], message)
        except OSError:
            return dict(state="pending", reason="probe_delivery_unknown", op_id=op_id)
    deadline = clock() + timeout
    while True:
        ack = _path(op_id, ".probe-ack.json")
        if ack.exists():
            receipt = json.loads(ack.read_text(encoding="utf-8"))
            current = store.get_orch(con, request["orch_id"])
            if (current is None or current["stopped_at"] is not None
                    or (current["session_id"], current["job_id"], current["socket"]) != (
                        request["session_id"], request["job_id"], request["socket"])):
                return dict(state="pending", reason="controller_changed", op_id=op_id)
            live = _live_controller(rt, current)
            if live is None:
                return dict(state="pending", reason="controller_not_verified_alive", op_id=op_id)
            receipt = dict(receipt, live_controller=live)
            if record["state"] == "needs_session_refresh":
                with entry_ops._lock_file() as handle:
                    try_lock(handle)
                    record = entry_ops.read(op_id)
                    if record["state"] == "needs_session_refresh":
                        record.update(state=record.get("restart_state", "done"), session_refresh="verified",
                                      session_probe=receipt, finished_at=time.time(),
                                      summary="Controller identity, liveness and visible session tools verified.",
                                      manual_recovery=None)
                        entry_ops._write(record)
            return receipt
        if clock() >= deadline:
            return dict(state="pending", reason="session_tools_not_verified", op_id=op_id)
        sleep(min(0.25, max(0, deadline - clock())))
