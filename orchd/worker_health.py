"""Read-only diagnosis of open tasks whose worker died without reporting (see docs/runbooks/interrupted-worker.md).

Nothing here writes to the DB, stops a worker, or touches a worktree: it labels a task and suggests manual steps.
"""

WATCHED = ("running", "acked")  # a worker owes a report in these states
FINISHED = ("done", "blocked")


def has_report(con, task_id):
    return con.execute("SELECT 1 FROM messages WHERE task_id=? AND kind='report' LIMIT 1", (task_id,)).fetchone() is not None


def assess(status, worker_alive, reported, *, task_id=None, worktree=None, branch=None, base=None):
    """Return dict(worker_health, recovery_hint).

    worker_health is "finished" (reported, so a gone worker is a normal exit), "alive", "unknown" (worker_alive is
    None: the query failed or a Codex thread is between turns; never guessed from timestamps), "orphan" (running or
    acked, worker confirmed dead, no report), or None for statuses outside this check (starting, failed, question).
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
