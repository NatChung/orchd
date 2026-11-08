"""Operator-requested lifecycle and instance operations from the Desktop entry.

The interface has no shell, so it cannot run `orchd orch restart` or `orchd upgrade` itself. These run in a
detached `orchd _entry-op` process instead: it outlives the MCP tool call (an upgrade can take longer than the
interface's tool timeout) and the entry MCP server itself. Each step is a fresh `orchd` process started through
the stable launcher, so a restart after an upgrade already runs the newly installed code.

One operation runs at a time (a lock file). Each one is a JSON record under <orchd home>/entry-ops; the entry
reads it back with orch_op_status.
"""
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

from . import paths, store
from .file_lock import try_lock

KINDS = ("restart", "upgrade", "instance_check", "instance_apply")
OUTPUT_TAIL = 4000  # chars of each step's output kept in the record
STEP_TIMEOUT = 900  # seconds; uv install with --refresh and a restart both finish well inside this


def ops_dir():
    return store.home() / "entry-ops"


def _record_path(op_id):
    if not isinstance(op_id, str) or not re.fullmatch(r"[0-9a-f]{12}", op_id):
        raise ValueError("op_id must be the 12-hex id returned by an entry operation")
    return ops_dir() / f"{op_id}.json"


def _write(record):
    from .upgrade import _atomic_json
    _atomic_json(_record_path(record["op_id"]), record)


def read(op_id):
    try:
        return json.loads(_record_path(op_id).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValueError(f"no entry operation {op_id}") from None


def latest():
    folder = ops_dir()
    records = []
    for file in folder.glob("*.json") if folder.is_dir() else ():
        try:
            records.append(json.loads(file.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return max(records, key=lambda r: r.get("requested_at", 0), default=None)


def _lock_file():
    folder = ops_dir()
    folder.mkdir(parents=True, exist_ok=True)
    return (folder / ".lock").open("a+")


def _busy():
    """True while an operation process holds the lock."""
    with _lock_file() as handle:
        try:
            try_lock(handle)
        except BlockingIOError:
            return True
    return False


def _spawn(argv, log):
    """Start a windowless process that survives the MCP server; leave the host's job object if allowed."""
    options = dict(stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, close_fds=True)
    if os.name != "nt":
        return subprocess.Popen(argv, start_new_session=True, **options)
    flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        return subprocess.Popen(argv, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB, **options)
    except OSError:  # the host's job forbids breakaway: still run, it only dies if the host is closed
        return subprocess.Popen(argv, creationflags=flags, **options)


def instance_source():
    """Resolve this instance's fixed source from its prior successful apply receipt."""
    from . import instances
    root = paths.root().absolute()
    receipt, current = instances.read_receipt(root)
    profile = receipt.get("profile")
    expected = {"ORCHD_ROOT": str(root), "ORCHD_HOME": str(store.home().absolute()),
                "ORCHD_CONFIG_DIR": str(paths.config_dir().absolute()),
                "ORCHD_PROJECTS": str(paths.projects_dir().absolute())}
    if not isinstance(profile, dict) or any(profile.get(key) != value for key, value in expected.items()):
        raise ValueError("Instance receipt does not match the active profile")
    source = receipt.get("source_repo")
    if not isinstance(source, str) or not Path(source).is_absolute():
        raise ValueError("Instance receipt has no fixed source repository; provision it from an Operator shell")
    return dict(source_repo=source, root=str(root), projects=profile["ORCHD_PROJECTS"],
                current_commit=receipt["source_commit"], expected_receipt=current)


def start(kind, *, entry_id, thread=None, restart=True, spawn=None, commit=None, expected_receipt=None):
    """Queue one operation and launch its runner. Refused while another one is running."""
    spawn = spawn or _spawn
    if kind not in KINDS:
        raise ValueError(f"unknown entry operation {kind}")
    instance = None
    if kind.startswith("instance_"):
        if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise ValueError("commit must be a full lowercase 40-hex SHA")
        instance = instance_source()
        if kind == "instance_apply" and expected_receipt != instance["expected_receipt"]:
            raise ValueError("Instance receipt changed; run instance_check again before applying")
        instance["commit"] = commit
    # The runner waits for this lock before it can update the operation record.
    # Save runner_pid before releasing it, so a fast result cannot be overwritten.
    with _lock_file() as handle:
        try:
            try_lock(handle)
        except BlockingIOError:
            current = latest()
            raise ValueError("another entry operation is still running"
                             + (f" (op {current['op_id']}, {current['kind']}); check it with orch_op_status" if current else "")) from None
        op_id = uuid.uuid4().hex[:12]
        log = ops_dir() / f"{op_id}.log"
        record = dict(op_id=op_id, kind=kind, restart=bool(restart) if kind == "upgrade" else True, state="queued",
                      entry_id=entry_id, requested_by_thread=thread, requested_at=time.time(), started_at=None,
                      finished_at=None, steps=[], summary=None, log=str(log))
        if instance is not None:
            record.update(instance=instance, restart=False)
        _write(record)
        with log.open("ab") as output:
            process = spawn([*paths.orchd_command(), "_entry-op", op_id], output)
        record["runner_pid"] = getattr(process, "pid", None)
        _write(record)
    return public(record)


def public(record):
    """What the entry sees: no log contents, only the summary and each step's outcome."""
    if record is None:
        return {"state": "none", "summary": "No entry operation has been requested from the interface yet."}
    keys = ("op_id", "kind", "restart", "state", "requested_at", "started_at", "finished_at", "summary",
            "old_orch", "new_orch", "adopted_tasks", "upgrade_lines", "error", "manual_recovery", "note",
            "instance", "instance_result")
    out = {k: record[k] for k in keys if k in record}
    out["steps"] = [{k: s[k] for k in ("step", "exit_code", "status") if k in s} for s in record.get("steps", [])]
    if record.get("state") in ("queued", "running") and (
            record.get("started_at") is not None or time.time() - record.get("requested_at", 0) > 60) and not _busy():
        out["state"] = "lost"
        out["summary"] = ("The runner process ended without recording a result; check `orchd orch list` and "
                          f"the log {record.get('log')} on this machine.")
    return out


def _run(argv):
    try:
        out = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                             timeout=STEP_TIMEOUT, stdin=subprocess.DEVNULL)
        return out.returncode, out.stdout or "", out.stderr or ""
    except subprocess.TimeoutExpired as error:
        return 124, str(error.stdout or ""), f"timed out after {STEP_TIMEOUT}s"
    except OSError as error:
        return 127, "", f"{type(error).__name__}: {error}"


def _json_object(text):
    """The first JSON object in a command's output (restart prints JSON, then maybe a plain line)."""
    start = text.find("{")
    if start < 0:
        return None
    try:
        return json.JSONDecoder().raw_decode(text[start:])[0]
    except ValueError:
        return None


def _restart_outcome(result):
    """(state, summary) from `orchd orch restart` JSON; a missed binding notice alone is only a warning."""
    if not isinstance(result, dict):
        return "failed", "orch restart printed no result; see the log on this machine."
    new = result.get("new_orch")
    adopted = (result.get("adopt") or {}).get("adopted") or []
    moved = f", moved {len(adopted)} open task(s)" if adopted else ""
    if result.get("status") == "done":
        return "done", f"Orch restarted: {result.get('old_orch')} -> {new}{moved}; the interface is bound to {new}."
    notice_only = (result.get("step") == "binding-status" and new
                   and (result.get("binding_status") or {}).get("orch_id") == new
                   and (result.get("new_health") or {}).get("state") == "alive"
                   and "binding notice" in (result.get("error") or ""))
    if notice_only:
        return "done_with_warnings", (f"Orch restarted: {result.get('old_orch')} -> {new}{moved}; the interface is "
                                      f"bound to {new}, but the bind notice to the new Orch was not confirmed "
                                      "(it learns about the binding from Operator's next message).")
    return "failed", (f"Orch restart stopped at step {result.get('step')}: {result.get('error')}. "
                      f"Manual recovery: {result.get('manual_recovery')}")


def run(op_id, *, run_command=_run):
    """Body of the detached `orchd _entry-op OP_ID` process."""
    record = read(op_id)
    with _lock_file() as handle:
        for attempt in range(20):  # a status read holds the lock for an instant
            try:
                try_lock(handle)
                break
            except BlockingIOError:
                time.sleep(0.1)
        else:
            record.update(state="failed", finished_at=time.time(),
                          summary="another entry operation was already running; nothing was done")
            _write(record)
            return 1
        record = read(op_id)
        record.update(state="running", started_at=time.time())
        _write(record)
        command = paths.orchd_command()
        try:
            if record["kind"].startswith("instance_"):
                from . import instances
                source = instance_source()
                saved = record["instance"]
                if source != {key: value for key, value in saved.items() if key != "commit"}:
                    raise ValueError("Instance receipt changed after this operation was queued; check again")
                result = instances.apply(saved["source_repo"], saved["commit"], saved["root"], saved["projects"],
                                         check=record["kind"] == "instance_check",
                                         expected_receipt=saved["expected_receipt"])
                record.update(state="done", instance_result=result,
                              summary="Instance checked; no files changed." if record["kind"] == "instance_check"
                              else "Instance applied; no sessions restarted or reloaded.")
                record["steps"].append(dict(step=record["kind"], exit_code=0, status="done"))
                return 0
            if record["kind"] == "upgrade":
                code, out, err = run_command([*command, "upgrade"])
                lines = [line for line in (out + "\n" + err).splitlines() if line.strip()]
                record["upgrade_lines"] = lines[-20:]
                record["steps"].append(dict(step="upgrade", exit_code=code, status="ok" if code == 0 else "failed",
                                            output=(out + err)[-OUTPUT_TAIL:]))
                if code:
                    record.update(state="failed", summary="orchd upgrade failed; the installed orchd is unchanged. "
                                                          + (lines[-1] if lines else ""))
                    return 1
                unchanged = any("already" in line and "newest" in line for line in lines)
                if not record.get("restart") or unchanged:
                    record.update(state="done", summary=("orchd is already up to date; Orch not restarted."
                                                         if unchanged else
                                                         "orchd upgraded; Orch not restarted (restart=false)."))
                    record["note"] = "Running Orchs keep the old code until restarted (orch_restart)."
                    return 0
            code, out, err = run_command([*command, "orch", "restart"])
            result = _json_object(out)
            state, summary = _restart_outcome(result)
            record["steps"].append(dict(step="restart", exit_code=code, status=state, output=(out + err)[-OUTPUT_TAIL:]))
            if isinstance(result, dict):
                record.update(old_orch=result.get("old_orch"), new_orch=result.get("new_orch"),
                              adopted_tasks=(result.get("adopt") or {}).get("adopted") or [])
                if state == "failed":
                    record.update(error=result.get("error"), manual_recovery=result.get("manual_recovery"))
            if record["kind"] == "upgrade" and state != "failed":
                summary = "orchd upgraded. " + summary
            record.update(state=state, summary=summary)
            if state != "failed":
                record["note"] = ("This interface conversation still runs the previous entry tools; open a new "
                                  "conversation (or restart the Codex app) to load updated ones.") \
                    if record["kind"] == "upgrade" else None
            return 0 if state != "failed" else 1
        except Exception as error:  # the record must never stay "running"
            record.update(state="failed", error=f"{type(error).__name__}: {error}",
                          summary=f"the operation crashed: {type(error).__name__}: {error}")
            return 1
        finally:
            record["finished_at"] = time.time()
            _write(record)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: orchd _entry-op OP_ID", file=sys.stderr)
        return 2
    return run(argv[0])
