"""Central goal records. Mutations and their before/after audit are one transaction."""
import json
import math
import re
import time
import uuid
from datetime import date, datetime, timezone

from . import store

TEXT_FIELDS = ("repo", "type", "intent", "pg", "sprint_goal", "done_when", "evidence", "authority",
               "status", "ball", "blocker", "source", "plan")
DATE_FIELDS = ("sprint_start", "sprint_end", "follow_up_date", "last_confirmed_date", "deadline",
               "last_progress_date", "waiting_nat_since")
FIELDS = TEXT_FIELDS + DATE_FIELDS + ("companies", "v", "j")
INPUT_FIELDS = FIELDS + ("linked_tasks",)


def validate(fields):
    unknown = set(fields) - set(INPUT_FIELDS)
    if unknown:
        raise ValueError(f"unknown goal fields: {', '.join(sorted(unknown))}")
    out = dict(fields)
    for key in TEXT_FIELDS:
        if key in out and not isinstance(out[key], str):
            raise ValueError(f"{key} must be text")
    if "repo" in out and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", out["repo"]):
        raise ValueError("repo must be a repo name, not a path or company")
    for key, choices in (("type", ("goal", "continuous")), ("status", ("active", "waiting", "paused", "done"))):
        if key in out and out[key] not in choices:
            raise ValueError(f"{key} must be one of {choices}")
    for key in DATE_FIELDS:
        if key in out and out[key] is not None:
            value = out[key]
            if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise ValueError(f"{key} must be YYYY-MM-DD or null")
            date.fromisoformat(value)
    if "v" in out and out["v"] is not None:
        value = out["v"]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 10:
            raise ValueError("v must be 0–10 or null (unknown); record Nat's value")
    if "j" in out and (type(out["j"]) is not int or out["j"] not in (1, 2, 3, 5, 8)):
        raise ValueError("j must be 1, 2, 3, 5 or 8")
    if "companies" in out:
        labels = out["companies"]
        if not isinstance(labels, list) or any(not isinstance(x, str) or not x.strip() for x in labels):
            raise ValueError("companies must be a list of non-empty labels")
        out["companies"] = json.dumps(labels, ensure_ascii=False)
    if "linked_tasks" in out:
        links = out["linked_tasks"]
        if not isinstance(links, list) or any(
            not isinstance(t, dict) or set(t) != {"task_id", "goal_critical"} or
            not isinstance(t["task_id"], str) or type(t["goal_critical"]) is not bool for t in links):
            raise ValueError("linked_tasks must be a list of {task_id, goal_critical: boolean}")
        if len({t["task_id"] for t in links}) != len(links):
            raise ValueError("linked_tasks contains duplicate task ids")
    return out


def _record(row):
    out = dict(row)
    out["companies"] = json.loads(out["companies"])
    return out


def get(con, goal_id, *, history=False):
    row = con.execute("SELECT * FROM goals WHERE id=?", (goal_id,)).fetchone()
    if row is None:
        raise KeyError(f"unknown goal {goal_id}")
    out = _record(row)
    out["linked_tasks"] = [dict(t) for t in con.execute(
        "SELECT id AS task_id, title, status, goal_critical FROM tasks WHERE goal_id=? ORDER BY created_at", (goal_id,))]
    if history:
        out["history"] = [dict(id=r["id"], actor=r["actor"], created_at=r["created_at"], changes=json.loads(r["changes"]))
                          for r in con.execute("SELECT * FROM goal_history WHERE goal_id=? ORDER BY id", (goal_id,))]
    return out


def list_goals(con, repo=None, status=None):
    clauses, values = [], []
    for key, value in (("repo", repo), ("status", status)):
        if value is not None:
            clauses.append(f"{key}=?")
            values.append(value)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    return [get(con, r["id"]) for r in con.execute("SELECT id FROM goals" + where + " ORDER BY repo, created_at, id", values)]


def set_goal(con, goal_id=None, *, actor, **fields):
    """No id creates a goal; an id updates an existing one. No-op updates add no history."""
    if not isinstance(actor, str) or not actor.strip():
        raise ValueError("goal change requires an actor")
    fields = validate(fields)
    links = fields.pop("linked_tasks", None)
    creating = goal_id is None
    if creating and "repo" not in fields:
        raise ValueError("new goal requires repo")
    now = time.time()
    with store.immediate(con):
        old = None if creating else get(con, goal_id)
        if old and "repo" in fields and fields["repo"] != old["repo"]:
            raise ValueError("a goal's repo is immutable; create a new goal")
        combined = {**(old or {}), **fields}
        if links is not None:
            for link in links:
                task = store.get_task(con, link["task_id"])
                if task["repo"] != combined["repo"]:
                    raise ValueError("goal and linked tasks must have the same repo")
                if task["goal_id"] not in (None, goal_id):
                    raise ValueError("task already belongs to another goal; remove its link there first")
        if combined.get("sprint_start") and combined.get("sprint_end") and combined["sprint_start"] > combined["sprint_end"]:
            raise ValueError("sprint_start must not follow sprint_end")
        if creating:
            goal_id = uuid.uuid4().hex[:12]
            fields.setdefault("last_progress_date", date.today().isoformat())
            values = dict(id=goal_id, **fields, created_at=now, updated_at=now)
            con.execute(f"INSERT INTO goals({','.join(values)}) VALUES({','.join('?' for _ in values)})", tuple(values.values()))
        else:
            changes = {k: v for k, v in fields.items() if v != (json.dumps(old[k], ensure_ascii=False) if k == "companies" else old[k])}
            if changes:
                con.execute(f"UPDATE goals SET {','.join(k+'=?' for k in changes)}, updated_at=? WHERE id=?",
                            (*changes.values(), now, goal_id))
        if links is not None:
            # Explicit replacement, including [] to unlink all; omitted leaves links alone.
            con.execute("UPDATE tasks SET goal_id=NULL, goal_critical=NULL WHERE goal_id=?", (goal_id,))
            for link in links:
                con.execute("UPDATE tasks SET goal_id=?, goal_critical=? WHERE id=?",
                            (goal_id, link["goal_critical"], link["task_id"]))
        new = get(con, goal_id)
        changed = {k: {"before": None if creating else old[k], "after": new[k]}
                   for k in FIELDS if creating or old[k] != new[k]}
        if links is not None:
            before = [] if creating else _links(old)
            after = _links(new)
            if before != after:
                changed["linked_tasks"] = dict(before=before, after=after)
        if changed:
            con.execute("UPDATE goals SET updated_at=? WHERE id=?", (now, goal_id))
            con.execute("INSERT INTO goal_history(goal_id,actor,created_at,changes) VALUES(?,?,?,?)",
                        (goal_id, actor, now, json.dumps(changed, ensure_ascii=False)))
    return get(con, goal_id, history=True)


def _links(goal):
    return sorted([dict(task_id=t["task_id"], goal_critical=None if t["goal_critical"] is None else bool(t["goal_critical"]))
                   for t in goal["linked_tasks"]], key=lambda t: t["task_id"])


def record_dispatch(con, goal_id, task_id, actor):
    if goal_id is None:
        return
    after = _links(get(con, goal_id))
    before = [t for t in after if t["task_id"] != task_id]
    now = time.time()
    con.execute("UPDATE goals SET updated_at=? WHERE id=?", (now, goal_id))
    con.execute("INSERT INTO goal_history(goal_id,actor,created_at,changes) VALUES(?,?,?,?)",
                (goal_id, actor, now, json.dumps({"linked_tasks": dict(before=before, after=after)})))


def validate_link(con, repo, goal_id, goal_critical):
    if goal_critical is not None and type(goal_critical) is not bool:
        raise ValueError("goal_critical must be boolean or null")
    if goal_id is None:
        if goal_critical is not None:
            raise ValueError("goal_critical requires goal_id")
        return
    goal = get(con, goal_id)
    if goal["repo"] != repo:
        raise ValueError("goal and task must have the same repo")


def score(goal, today=None, *, progress_date=None, nat_since=None):
    """Provisional 10-day deadline ramp; elapsed calendar days clamp at 10. Unknown V stays unknown."""
    today = today or date.today()
    def elapsed(value):
        return min(10, max(0, (today - date.fromisoformat(value)).days)) if value else 0
    deadline = goal.get("deadline") or goal.get("sprint_end")
    t = min(10, max(0, 10 - (date.fromisoformat(deadline) - today).days)) if deadline else 0
    last_progress = max(filter(None, (goal.get("last_progress_date"), progress_date)), default=None)
    waiting = min(filter(None, (goal.get("waiting_nat_since") if goal.get("ball", "").lower() == "nat" else None, nat_since)), default=None)
    parts = dict(V=goal.get("v"), T=t, S=elapsed(last_progress), B=elapsed(waiting), J=goal["j"])
    parts["score"] = None if parts["V"] is None else (parts["V"] + t + parts["S"] + parts["B"]) / parts["J"]
    return parts


def export_md(con, repo=None):
    lines = ["# Orch goals", "", "Central orchd snapshot; goal done and worker report done are separate states.", ""]
    for goal in list_goals(con, repo):
        lines += [f"## {goal['repo']} · {goal['id']}", ""]
        for key in FIELDS:
            value = goal[key]
            if isinstance(value, list):
                value = ", ".join(value)
            lines += [f"- **{key}**: {value if value is not None else 'unknown'}"]
        lines += ["", "### Linked tasks", ""]
        for task in goal["linked_tasks"]:
            lines.append(f"- {task['task_id']} · {task['status']} · critical={task['goal_critical']}: {task['title']}")
        lines += ["", "### Change history", ""]
        for item in get(con, goal["id"], history=True)["history"]:
            stamp = datetime.fromtimestamp(item["created_at"], timezone.utc).isoformat()
            lines.append(f"- {stamp} · {item['actor']}: {json.dumps(item['changes'], ensure_ascii=False)}")
        lines.append("")
    return "\n".join(lines)
