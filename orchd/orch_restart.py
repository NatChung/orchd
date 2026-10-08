"""Operator-only restart sequence. Never infers death from registry bookkeeping."""
from . import core, entry, inventory, store
from .orch_health import owner_health
from .runtime import ORCH_MODELS


def restart(con, rt, old_id=None, model=None, dry_run=False):
    result = dict(status="failed", step="select", old_orch=old_id, new_orch=None, steps=[])
    remedy = "orchd orchs --all; orchd orch-restart OLD_ID --dry-run"
    try:
        bound = con.execute("SELECT orch_id FROM entries WHERE id='desktop'").fetchone()
        if old_id is None:
            if bound is None or store.get_orch(con, bound["orch_id"]) is None:
                result["candidates"] = inventory.list_orchs(con, rt, observe=False)
                raise ValueError("Desktop binding has no registered Orch; choose an explicit OLD_ID")
            old_id = bound["orch_id"]
        result["old_orch"] = old_id
        old = store.get_orch(con, old_id)
        if old is None or old["kind"] != "claude" or not old["job_id"]:
            raise ValueError("OLD_ID must identify a registered Claude Orch with a job")
        model = model or next((k for k, v in ORCH_MODELS.items() if v == old["model"]), None)
        if model not in ORCH_MODELS:
            raise ValueError("old model is unsupported; specify --model explicitly")
        rebind = bound is not None and bound["orch_id"] == old_id
        tasks = [t["id"] for t in store.open_tasks(con) if t["orch_thread"] == old_id]
        result.update(model=model, open_tasks=tasks, rebind_desktop=rebind)
        if dry_run:
            result.update(status="dry-run", step="plan", plan=[
                f"orchd orch-stop {old_id}",
                "confirm old job is dead with a fresh runtime and socket probe; stop if uncertain",
                f"orchd orch --model {model} --no-attach",
                f"orchd adopt NEW_ID --from {old_id}; check committed and notification errors"
                if tasks else "skip adopt if there are still no open tasks",
                "orchd binding --to NEW_ID; orchd binding --status" if rebind else "leave Desktop binding alone",
            ])
            return result

        result["step"] = "stop"
        remedy = f"orchd orchs --all; orchd orch-stop {old_id}"
        core.stop_orch(con, rt, old_id)
        result["steps"].append(dict(step="stop", status="requested"))

        result["step"] = "confirm-dead"
        stopped = store.get_orch(con, old_id)
        health = owner_health(stopped, rt.live_jobs())
        result["old_health"] = health
        if health["state"] != "dead":
            raise ValueError("old Orch is not confirmed dead; no replacement was started")
        if stopped["socket"]:
            listening = rt.socket_listening(stopped["socket"])
            result["old_socket_listening"] = listening
            if listening is not False:
                raise ValueError("old socket is listening or unverified; no replacement was started")
        result["steps"].append(dict(step="confirm-dead", status="dead"))

        result["step"] = "start"
        remedy = f"orchd orchs --all; orchd orch --model {model} --no-attach"
        new = core.start_orch(con, rt, model)
        new_id = new["id"]
        result["new_orch"] = new_id
        result["steps"].append(dict(step="start", orch_id=new_id))

        result["step"] = "adopt"
        remedy = f"orchd list; orchd adopt {new_id} --from {old_id}"
        # Re-read after stopping/starting: workers may have reported meanwhile.
        tasks = [t["id"] for t in store.open_tasks(con) if t["orch_thread"] == old_id]
        result["open_tasks"] = tasks
        if tasks:
            adoption = core.adopt(con, rt, new_id, from_orch=old_id)
            result["adopt"] = adoption
            if adoption.get("committed") is not True:
                raise ValueError("adopt did not confirm its commit")
            if (adoption.get("notification_errors") or adoption.get("new_owner_error")
                    or adoption.get("error_recording_failures") or adoption.get("superseded")
                    or set(adoption.get("adopted", [])) != set(tasks)):
                remedy = (f"orchd list; orchd attach {new_id}; tell the new Orch to read inbox; "
                          "check current task owners before retrying adopt (the move already committed)")
                raise ValueError("adopt committed with notification errors or changed task ownership")
        else:
            result["adopt"] = dict(skipped=True, reason="no open tasks")
        result["steps"].append(dict(step="adopt", **result["adopt"]))

        if rebind:
            result["step"] = "binding"
            remedy = f"orchd binding --status; orchd binding --to {new_id}; orchd binding --status"
            current = con.execute("SELECT orch_id FROM entries WHERE id='desktop'").fetchone()
            if current is None or current["orch_id"] != old_id:
                raise ValueError("Desktop binding changed during restart; inspect it before rebinding")
            result["binding"] = entry.bind(con, rt, new_id, force=True)
            result["step"] = "binding-status"
            result["binding_status"] = entry.snapshot(con, rt)
            if result["binding_status"]["orch_id"] != new_id:
                raise ValueError("Desktop binding read-back does not match the new Orch")
            if result["binding"].get("notice") != "sent":
                raise ValueError("binding committed but the new Orch's binding notice failed")
            result["steps"].append(dict(step="binding", **result["binding_status"]))
        result.update(status="done", step="done", first_prompt="盤點上次")
    except Exception as error:
        result.update(error=f"{type(error).__name__}: {error}", manual_recovery=remedy)
    return result
