"""Read-only diagnosis of open tasks whose worker died without reporting (see docs/runbooks/interrupted-worker.md).

Nothing here writes to the DB, stops a worker, or touches a worktree: it labels a task and suggests manual steps.
"""

WATCHED = ("running", "acked", "question")  # a worker owes a report (or is waiting on an answer) in these states
STALE_STATUSES = WATCHED
WORKER_KINDS = ("ack", "progress", "question", "report")  # messages the worker itself sends
DEFAULT_STALE_HOURS = 24
FINISHED = ("done", "blocked")


def has_report(con, task_id):
    return con.execute("SELECT 1 FROM messages WHERE task_id=? AND kind='report' LIMIT 1", (task_id,)).fetchone() is not None


def last_report_at(con, task_id):
    """Time of the latest ack/progress/question/report the worker sent, or None. Read-only."""
    row = con.execute("SELECT MAX(created_at) FROM messages WHERE task_id=? AND kind IN (?,?,?,?)",
                      (task_id, *WORKER_KINDS)).fetchone()
    return row[0]


def stale_hours(override=None):
    """Hours of silence before a task is labelled stale: argument, else ORCHD_STALE_HOURS, else the default."""
    import os
    for value in (override, os.environ.get("ORCHD_STALE_HOURS")):
        if value not in (None, ""):
            hours = float(value)
            if hours <= 0:
                raise ValueError("stale hours must be positive")
            return int(hours) if hours == int(hours) else hours
    return DEFAULT_STALE_HOURS


def is_stale(status, last_heard, now, hours):
    """Label only: a running/acked/question task with no worker message for `hours`. Never a verdict of death."""
    return status in STALE_STATUSES and last_heard is not None and now - last_heard > hours * 3600


def assess(status, worker_alive, reported, *, task_id=None, worktree=None, branch=None, base=None):
    """Return dict(worker_health, recovery_hint).

    worker_health is "finished" (reported, so a gone worker is a normal exit), "alive", "unknown" (worker_alive is
    None: the query failed or a Codex thread is between turns; never guessed from timestamps), "orphan" (running or
    acked or question, worker confirmed dead, no report), or None for statuses outside this check (starting, failed).
    worker_alive only says the process/job exists, never that it is doing work; see is_stale for silence.
    """
    if isinstance(worker_alive, str):
        worker_alive = None if worker_alive == "unknown" else worker_alive != "dead"
    if status in FINISHED or reported:
        return dict(worker_health="finished", recovery_hint=None)
    if status not in WATCHED:
        return dict(worker_health=None, recovery_hint=None)
    if worker_alive is None:
        return dict(worker_health="unknown", recovery_hint=None)
    if worker_alive:
        return dict(worker_health="alive", recovery_hint=None)
    return dict(worker_health="orphan", recovery_hint=recovery_hint(task_id, worktree, branch, base))


def recovery_hint(task_id, worktree, branch, base):
    if not worktree:
        return (f"Worker died without a report and task {task_id} has no worktree. The DB row is left as is; "
                f"the Orch decides whether to dispatch again with rework_of={task_id}.")
    since = f"{base}..HEAD" if base else "HEAD -5"
    return (f"Worker died without a report; the DB row is left as is. Before deciding, inspect what it left: "
            f"`git -C {worktree} status --porcelain`, `git -C {worktree} log --oneline {since}`, "
            f"`git -C {worktree} ls-remote origin refs/heads/{branch}`. Keep the worktree and branch {branch}: "
            f"uncommitted or unpushed work there is the only copy. Then the Orch dispatches a new task with "
            f"rework_of={task_id} that continues from that branch, or asks Operator. Do not force-remove the worktree; "
            f"see docs/runbooks/interrupted-worker.md.")
