"""Require the replacement Orch to exercise its tools before completing an upgrade."""
import json
import os
import time
import uuid
from contextlib import contextmanager

from . import entry_ops, store
from .file_lock import try_lock
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
        if (record["kind"] != "upgrade" or record["state"] not in ("running", "needs_session_refresh")
                or record.get("new_orch") != orch_id or request["orch_id"] != orch_id
                or orch is None or orch["kind"] != "claude" or orch["stopped_at"] is not None
                or orch["session_id"] != request["session_id"] or challenge != request["challenge"]):
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
    if any(os.environ.get(k) for k in ("ORCHD_ORCH_ID", "ORCHD_TASK_ID", "ORCHD_WORKTREE")):
        raise PermissionError("Session verification is an Operator-only maintenance action")
    message = None
    with _lock(op_id):
        record = entry_ops.read(op_id)
        if record["kind"] != "upgrade" or record["state"] not in ("running", "needs_session_refresh"):
            raise ValueError("Only an unfinished upgrade may verify session tools")
        orch = store.get_orch(con, record.get("new_orch"))
        if orch is None or orch["kind"] != "claude" or orch["stopped_at"] is not None:
            raise ValueError("Upgrade has no active replacement Claude Orch")
        path = _path(op_id, ".probe.json")
        if path.exists():
            request = json.loads(path.read_text(encoding="utf-8"))
            if (request["orch_id"], request["session_id"], request["dispatch_fields"]) != (
                    orch["id"], orch["session_id"], dispatch_fields()):
                raise ValueError("Upgrade controller or schema changed; do not reuse the probe")
        else:
            request = dict(orch_id=orch["id"], session_id=orch["session_id"],
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
                    or current["session_id"] != request["session_id"]):
                return dict(state="pending", reason="controller_changed", op_id=op_id)
            if record["state"] == "needs_session_refresh":
                with entry_ops._lock_file() as handle:
                    try_lock(handle)
                    record = entry_ops.read(op_id)
                    if record["state"] == "needs_session_refresh":
                        record.update(state=record.get("restart_state", "done"), session_refresh="verified",
                                      session_probe=receipt, finished_at=time.time(),
                                      summary="Package update, Orch restart and session tools verified.",
                                      manual_recovery=None)
                        entry_ops._write(record)
            return receipt
        if clock() >= deadline:
            return dict(state="pending", reason="session_tools_not_verified", op_id=op_id)
        sleep(min(0.25, max(0, deadline - clock())))
