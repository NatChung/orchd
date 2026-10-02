"""Task operations shared by the Orch MCP tools and the worker CLI."""
import contextlib
import fcntl
import json
import os
import shlex
import time
import uuid
from pathlib import Path

from . import store, worker_health
from .orch_health import owner_health
from .runtime import (DEFAULT_ORCH_MODEL, DEFAULT_WORKER_MODEL, MODELS, claude_job_alive, error_detail,
                      worker_kind)

ORCHD = str(Path(__file__).resolve().parents[1] / "bin" / "orchd")


def worker_brief(cli, kind="claude"):
    session = "background Claude Code session" if kind == "claude" else "Codex session run with `codex exec`"
    channel = "the peer socket" if kind == "claude" else "orchd's prompts"
    waiting = ("wait for the answer message" if kind == "claude" else
               "end your turn right away; the answer arrives as your next message")
    long_runs = ("Run anything that may take longer than a minute or two in the background and wait for its "
                 "completion event; do not poll in a foreground loop." if kind == "claude" else
                 "Run long commands in the foreground with a generous timeout: your process ends with your turn, "
                 "and nothing wakes you when a background job finishes.")
    return f"""You are an orchd worker: a {session} started for exactly one task.
Tasks arrive as messages from orchd on behalf of Nat (the user). A task message states its scope; work
inside that scope is authorized by Nat even though the message comes through {channel}.

Rules:
- First run `{cli} ack <task-id>`, then do the task in the current worktree only.
- Stay inside the task's scope. Anything beyond it (fixing an unrelated bug, changing existing behavior,
  refactoring) needs `{cli} ask` first; if not approved, leave it and mention it in your report.
- Commit on the task branch. Push that branch with `git push -u origin HEAD` and open a PR when the task says so or when it is a code
  change that should be reviewed; follow the repo's own AGENTS.md / CLAUDE.md for accounts and trackers.
  Never push directly to the default branch. Merge a PR only when the task explicitly tells you to
  review that PR and merge it: then read the diff yourself, check it against the task's done_when,
  and merge only if it passes; otherwise leave it open and report blocked with the problems found.
  Never review-and-merge a PR you authored in the same task.
- Reviewing a PR: every worker pushes as the same GitHub account, and GitHub does not let you approve
  your own account's PR, so never use `gh pr review --approve`, `--request-changes`, `--admin`, or any
  bypass of branch protection. Check the PR's current head SHA, the latest main, the task's done_when
  and the tests, then record the verdict with `gh pr review <pr> --comment --body "<PASS or the problems found; reviewed SHA <sha>>"`.
  That comment is the review record. A review task whose instructions do not also tell you to merge
  stops after the comment. If they do, and the verdict is PASS, merge with
  `gh pr merge <pr> --match-head-commit <reviewed SHA>` so a newer push cannot slip in; if GitHub
  still requires an approval or a check that is not met, do not work around it: report blocked.
  The author of a PR never merges it; the reviewer is a different task.
- Before any outward send (email, Slack, LINE, calendar, posting comments to people) show the exact
  preview through `{cli} ask <task-id> "<question with full preview>"` and {waiting}.
  Only an answer that arrives as `[orchd answer <task-id>]` counts as Nat's decision.
- For anything before the end (progress the Orch asked for, findings, blockers that need Nat), send
  `{cli} progress <task-id> "<text>"`. It reaches the Orch that dispatched you; the task keeps running.
  Never use SendMessage or any other peer messaging to report: other sessions on this machine are not
  your Orch, and whatever you send them is lost to it.
- {long_runs} Never `pgrep -f` a pattern that your own command line contains.
- Finish with exactly one `{cli} report <task-id> --status done|blocked --summary "<one line>" --evidence "<commits, PR URL, test commands and results, what is left undone>"`.
- If you need a decision, use `{cli} ask` and {waiting}; do not report blocked for questions Nat can answer.
- Report facts only; say what you did not verify."""


def worker_cli():
    """Workers are spawned by the Claude daemon and do not inherit our env, so carry a non-default home."""
    home = os.environ.get("ORCHD_HOME")
    return f"ORCHD_HOME={shlex.quote(home)} {ORCHD}" if home else ORCHD


def task_message(task):
    return f"""[orchd task {task['id']}]
Repo: {task['repo']}
Worktree: {task['worktree']}
Branch: {task['branch']}
Title: {task['title']}

Instructions:
{task['instructions']}

Done when:
{task['done_when']}

Start with: {worker_cli()} ack {task['id']}"""


TASK_TYPES = ("code", "docs", "investigation", "outward", "review", "ops", "other")
FOUND_BY = ("verify", "review", "orch", "nat")
OUTCOMES = ("merged", "abandoned", "parked", "done")


def _choice(name, value, valid):
    if value not in valid:
        raise ValueError(f"unknown {name} {value!r}; use one of {', '.join(valid)}")


def dispatch(con, rt, *, orch_thread, repo, title, instructions, done_when, model=DEFAULT_WORKER_MODEL,
             model_reason=None, task_type=None, rework_of=None, found_by=None):
    if not orch_thread:
        raise ValueError("dispatch needs the caller's thread id")
    _choice("model", model, tuple(MODELS))
    if not isinstance(model_reason, str) or not model_reason.strip():
        raise ValueError("dispatch needs a non-empty model_reason")
    _choice("task_type", task_type, TASK_TYPES)
    if found_by is not None:
        if not rework_of:
            raise ValueError("found_by is only allowed with rework_of")
        _choice("found_by", found_by, FOUND_BY)
    if rework_of:
        try:
            store.get_task(con, rework_of)
        except KeyError:
            raise ValueError(f"rework_of: unknown task {rework_of}") from None
    repo_path = rt.repo_path(repo)
    kind = worker_kind(MODELS[model])
    if kind == "claude" and not rt.claude_trusted(repo_path):
        raise ValueError(f"Claude has not trusted {repo_path}. Ask Nat to run `claude` there once and accept "
                         "the trust prompt, then dispatch again.")
    task_id = store.new_task_id()
    task = store.create_task(con, id=task_id, repo=repo, repo_path=str(repo_path), title=title,
                             instructions=instructions, done_when=done_when,
                             orch_thread=orch_thread, codex_bin=rt.codex, model=MODELS[model],
                             model_reason=model_reason, task_type=task_type, rework_of=rework_of, found_by=found_by)
    store.add_message(con, task_id, "dispatch", json.dumps(
        dict(model=MODELS[model], model_reason=model_reason, task_type=task_type,
             rework_of=rework_of, found_by=found_by), ensure_ascii=False))
    try:
        base, branch, worktree = rt.create_worktree(repo_path, repo, task_id)
        store.update_task(con, task_id, base=base, branch=branch, worktree=worktree)
        if kind == "codex":  # no system-prompt flag and no socket: the brief leads the first turn's prompt
            task = store.get_task(con, task_id)
            job, session = rt.start_codex_worker(worktree, rt.codex_log(task_id),
                                                 worker_brief(worker_cli(), kind) + "\n\n" + task_message(task),
                                                 MODELS[model])
            store.update_task(con, task_id, job_id=job, session_id=session, status="running")
        else:
            sock = rt.socket_path(task_id)
            job, session = rt.start_worker(worktree, sock, worker_brief(worker_cli()), MODELS[model])
            store.update_task(con, task_id, socket=sock, job_id=job, session_id=session, status="running")
            task = store.get_task(con, task_id)
            rt.send_uds(sock, session, task_message(task))
    except Exception as error:
        store.update_task(con, task_id, status="failed", note=f"{type(error).__name__}: {error}"[:1000])
        worktree = store.get_task(con, task_id)["worktree"]
        if worktree:  # nothing has run in it yet, so it holds no work
            try:
                rt.remove_worktree(repo_path, worktree)
            except Exception:
                pass
        raise
    return store.get_task(con, task_id)


def other_open_on_repo(con, repo, orch_thread, exclude_id=None):
    return [dict(task_id=t["id"], title=t["title"], orch_thread=t["orch_thread"], status=t["status"])
            for t in store.open_tasks(con)
            if t["repo"] == repo and t["orch_thread"] != orch_thread and t["id"] != exclude_id]


def _short(text, limit=160):
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0] if " " in text[:limit] else text[:limit]
    return cut + "…"


def task_model(task):
    """The task's stored full model id; "unknown" for tasks dispatched before models were stored (null/empty)."""
    model = task["model"]
    return model.strip() if isinstance(model, str) and model.strip() else "unknown"


def wake_text(task, line):
    """Notification text; carries the task's stored model so the Orch can tell Claude from Codex workers."""
    model = task_model(task)
    return f"[orchd] {task['repo']}/{task['id']} ({model}) {line} — 請呼叫 orchd 的 inbox 工具讀取。"


def _wake(con, rt, task, message_id, line):
    try:
        with store.task_delivery(con, [task["id"]]):
            task = store.get_task(con, task["id"])
            con.execute("UPDATE messages SET recipient_orch=? WHERE id=?", (task["orch_thread"], message_id))
            _notify_orch(con, rt, task["orch_thread"], task["codex_bin"], wake_text(task, line))
    except Exception as error:  # the message is already stored; list_open still shows it
        con.execute("UPDATE messages SET wake_error=? WHERE id=?", (f"{type(error).__name__}: {error}"[:500], message_id))
        return False
    return True


def ack(con, task_id):
    task = store.get_task(con, task_id)
    if task["status"] == "running":
        store.update_task(con, task_id, status="acked")
    store.add_message(con, task_id, "ack", "worker acknowledged the task")


def report(con, rt, task_id, status, summary, evidence):
    if status not in ("done", "blocked"):
        raise ValueError("status must be done or blocked")
    task = store.get_task(con, task_id)
    mid = store.add_message(con, task_id, "report", f"{status}: {summary}", evidence)
    store.update_task(con, task_id, status=status)
    return _wake(con, rt, task, mid, f"{status}: {_short(summary)}")


def progress(con, rt, task_id, text):
    """Interim update the Orch asked for or should know; the task keeps running."""
    task = store.get_task(con, task_id)
    mid = store.add_message(con, task_id, "progress", text)
    return _wake(con, rt, task, mid, f"progress: {_short(text.splitlines()[0] if text.strip() else text)}")


def ask(con, rt, task_id, question):
    task = store.get_task(con, task_id)
    mid = store.add_message(con, task_id, "question", question)
    store.update_task(con, task_id, status="question")
    return _wake(con, rt, task, mid, f"question: {_short(question.splitlines()[0])}")


def inbox(con, orch_thread):
    rows = store.unread_for_thread(con, orch_thread)
    store.mark_read(con, [r["id"] for r in rows])
    return [dict(task_id=r["task_id"], repo=r["repo"], title=r["title"], kind=r["kind"],
                 body=r["body"], evidence=r["evidence"], task_status=r["status"],
                 model=task_model(r)) for r in rows]


class _ResumeFailed(Exception):
    pass


def answer(con, rt, task_id, text=None, flush=False):
    """Deliver an answer, or queue it while a Codex worker is mid-turn.

    Returns {status: delivered|queued|failed, delivered: n, pending: n}. A Codex worker cannot take a message
    mid-turn, so its answers wait in FIFO order until a later `answer` (or `flush=True`) finds it between
    turns; they then go out together as one new turn. Nothing else sends them (see docs/decisions.md)."""
    task = store.get_task(con, task_id)
    if task["status"] == "closed":
        raise ValueError(f"task {task_id} is closed")
    if text is None and not flush:
        raise ValueError("answer needs text, or flush=true to send queued answers")
    if worker_kind(task["model"]) != "codex":
        if text is None:
            return dict(status="delivered", delivered=0, pending=0)
        if not task["socket"] or not task["session_id"]:
            raise ValueError(f"task {task_id} has no running worker")
        rt.send_uds(task["socket"], task["session_id"], f"[orchd answer {task_id}]\n{text}")
        store.add_message(con, task_id, "answer", text)
        store.update_task(con, task_id, status="acked")
        return dict(status="delivered", delivered=1, pending=0)
    if not task["session_id"] or not task["worktree"]:
        raise ValueError(f"task {task_id} has no codex thread")
    if text is not None:  # stored before any attempt, so a busy turn or a failed resume loses nothing
        store.add_message(con, task_id, store.QUEUED, text)
    for _ in range(120):  # `orchd ask` wakes the Orch before the worker's turn has finished exiting
        if not (task["job_id"] and rt.pid_alive(task["job_id"])):
            break
        rt.sleep(0.5)
        task = store.get_task(con, task_id)
    try:
        with store.immediate(con):
            task = store.get_task(con, task_id)  # re-read under the lock: another call may have resumed or closed
            if task["status"] == "closed":
                raise ValueError(f"task {task_id} is closed; its queued answers stay undelivered")
            pending = store.pending_answers(con, task_id)
            if not pending:  # nothing waits, even if a concurrent flush sent this call's text
                return dict(status="delivered", delivered=0, pending=0)
            if task["job_id"] and rt.pid_alive(task["job_id"]):
                return dict(status="queued", delivered=0, pending=len(pending))
            message = "\n\n".join(f"[orchd answer {task_id}]\n{row['body']}" for row in pending)
            try:
                job = rt.resume_codex_worker(task["worktree"], rt.codex_log(task_id), task["session_id"], message,
                                             task["model"])
            except Exception as error:
                raise _ResumeFailed(error) from error
            store.mark_read(con, [row["id"] for row in pending])
            for row in pending:
                store.add_message(con, task_id, "answer", row["body"])
            store.update_task(con, task_id, job_id=job, status="acked")
    except _ResumeFailed as failed:  # rolled back: every answer is still queued for the next flush
        error = failed.__cause__
        return dict(status="failed", delivered=0, pending=len(store.pending_answers(con, task_id)),
                    error=f"{type(error).__name__}: {error}"[:500])
    return dict(status="delivered", delivered=len(pending), pending=0)


def list_open(con, rt):
    try:
        jobs = rt.live_jobs()
    except Exception:  # a failed status probe is not proof an Orch has exited
        jobs = None
    out = []
    for t in store.open_tasks(con):
        if worker_kind(t["model"]) == "codex":  # between turns there is no process, only a resumable thread
            alive = True if t["job_id"] and rt.pid_alive(t["job_id"]) else (
                None if t["status"] in ("question", "done", "blocked") else False)
        else:
            alive = claude_job_alive(jobs, t["job_id"])
        out.append(dict(task_id=t["id"], repo=t["repo"], title=t["title"], status=t["status"],
                        model=task_model(t), worker_alive=alive, worktree=t["worktree"], branch=t["branch"],
                        orch_thread=t["orch_thread"], note=t["note"],
                        owner_health=owner_health(store.get_orch(con, t["orch_thread"]), jobs),
                        notification_delivery=store.notification_delivery(con, t["id"]),
                        **worker_health.assess(t["status"], alive, worker_health.has_report(con, t["id"]),
                                               task_id=t["id"], worktree=t["worktree"], branch=t["branch"],
                                               base=t["base"])))
    return out


def _record_usage(con, rt, task):
    if not task["session_id"]:
        return
    try:
        usage = (rt.codex_usage if worker_kind(task["model"]) == "codex" else rt.claude_usage)(task["session_id"])
    except Exception:  # usage is bookkeeping; never block a close
        return
    if usage:
        store.add_message(con, task["id"], "usage", json.dumps(dict(model=task["model"], **usage)))


CLOSE_LOCK_WAIT = 120  # seconds a close waits for the task's delivery lock before giving up


def close(con, rt, task_id, outcome=None, rating=None, lock_wait=CLOSE_LOCK_WAIT):
    """Stop the worker; remove the worktree only when nothing local would be lost.
    The task is closed only after its worker is confirmed stopped and its worktree removed or deliberately kept.
    Anything short of that raises, leaves status, worktree and events as they were and notes why, so close can
    simply be run again. Concurrent closes of one task run one at a time and write a single close event."""
    if store.get_task(con, task_id)["status"] == "closed":
        return dict(task_id=task_id, closed=True, worktree="already closed")
    if outcome is not None:
        _choice("outcome", outcome, OUTCOMES)
    if rating is not None and (isinstance(rating, bool) or not isinstance(rating, int) or not 1 <= rating <= 3):
        raise ValueError("rating must be an integer 1-3")
    locked = False
    try:
        with store.task_delivery(con, [task_id], timeout=lock_wait):
            locked = True
            return _close_locked(con, rt, task_id, outcome, rating)
    except TimeoutError:
        if locked:
            raise
        raise RuntimeError(f"close of {task_id} waited {lock_wait:g}s for the task's delivery lock (another close, "
                           "retry or adopt holds it); nothing changed, run close again") from None


def _close_locked(con, rt, task_id, outcome, rating):
    """close's work under the task's delivery lock. The lock is not re-entrant: nothing here may take it again
    (no _wake, no adopt)."""
    task = store.get_task(con, task_id)
    if task["status"] == "closed":  # another close finished while we waited for the lock
        return dict(task_id=task_id, closed=True, worktree="already closed")
    fields = {k: v for k, v in (("outcome", outcome), ("rating", rating)) if v is not None}
    if fields:
        store.update_task(con, task_id, **fields)
    kept, step = None, f"worker {task['job_id']} not confirmed stopped"
    try:
        if task["job_id"]:
            rt.stop_task_worker(worker_kind(task["model"]), task["job_id"],
                                (task["worktree"], task["session_id"]))
        step = "worktree check or removal failed"
        if task["worktree"]:
            removable, reason = rt.worktree_state(task["worktree"], task["base"])
            kept = rt.remove_worktree(task["repo_path"], task["worktree"], task["base"]) if removable else reason
    except Exception as e:  # not closed: status stays as it was, so close can simply be run again
        detail = error_detail(e)
        store.update_task(con, task_id, note=f"close pending, {step}, worktree kept (run close again): {detail}")
        raise RuntimeError(f"close of {task_id} not finished, {step}"
                           + (f", worktree {task['worktree']} left in place" if task["worktree"] else "")
                           + f": {detail}") from None
    con.execute("BEGIN IMMEDIATE")  # usage + close event + terminal status land together or not at all
    try:
        task = store.get_task(con, task_id)
        if task["status"] == "closed":  # closed meanwhile by a caller outside this lock (an older orchd)
            con.execute("ROLLBACK")
            return dict(task_id=task_id, closed=True, worktree="already closed")
        stale = (task["note"] or "").startswith("close pending")
        _record_usage(con, rt, task)
        store.add_message(con, task_id, "close", json.dumps(dict(outcome=task["outcome"], rating=task["rating"])))
        store.update_task(con, task_id, status="closed",
                          note=(f"worktree kept: {kept}" if kept else None if stale else task["note"]))
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise
    return dict(task_id=task_id, closed=True,
                worktree=f"kept at {task['worktree']} ({kept})" if kept else "removed")


def view(con, rt, task_id):
    task = store.get_task(con, task_id)
    if not task["job_id"]:
        raise ValueError(f"task {task_id} has no worker")
    if worker_kind(task["model"]) == "codex":
        if rt.pid_alive(task["job_id"]):
            raise ValueError(f"task {task_id}'s codex worker is in a turn; `orchd watch` shows its progress")
        rt.open_codex_viewer(task["worktree"], task["session_id"])
        return f"opened Ghostty: codex resume {shlex.quote(task['session_id'])}"
    rt.open_viewer(task["job_id"])
    return f"opened Ghostty: claude attach {shlex.quote(task['job_id'])}"


def orch_home():
    return Path(os.environ.get("ORCHD_ORCH_HOME", Path.home() / "projects" / "orch"))


def start_orch(con, rt, model_key=DEFAULT_ORCH_MODEL):
    if model_key not in MODELS or worker_kind(MODELS[model_key]) != "claude":
        raise ValueError(f"unknown model {model_key!r}; use one of {', '.join(MODELS)}")
    home = orch_home()
    if not rt.claude_trusted(home):
        raise ValueError(f"Claude has not trusted {home}. Run `claude` in that directory once and accept "
                         "the trust prompt, then run `orchd orch` again.")
    orch_id = "o" + uuid.uuid4().hex[:7]
    sock, job, session = rt.start_orch(orch_id, MODELS[model_key], home)
    return store.register_orch(con, orch_id, "claude", model=MODELS[model_key], socket=sock,
                               session_id=session, job_id=job)


def stop_orch(con, rt, orch_id):
    orch = store.get_orch(con, orch_id)
    if orch is None:
        raise ValueError(f"unknown orch {orch_id}")
    if orch["job_id"]:
        rt.stop_worker(orch["job_id"])
    store.stop_orch(con, orch_id)


def _notify_orch(con, rt, orch_id, codex_bin, text):
    orch = store.get_orch(con, orch_id)
    if orch is not None and orch["kind"] == "claude":
        rt.send_uds(orch["socket"], orch["session_id"], text)
    else:  # Codex Orch, or an owner that was never registered
        rt.wake_orch(codex_bin, orch_id, text)


def _adopt_summary(con, task):
    parts = [f"status={task['status']}"]
    for kind in ("question", "report"):  # read or not: a read-but-unanswered question must not vanish
        m = store.latest_message(con, task["id"], kind)
        if m is not None:
            parts.append(f"latest {kind}: {_short(m['body'])}")
    return "; ".join(parts)


def adopt(con, rt, new_orch, task_ids=(), from_orch=None, force=False):
    """Operator-run (CLI) transfer of open tasks to another Orch. Never automatic.

    An old owner that is alive or unknown is refused unless force; only a confirmed-dead one moves freely.
    Unknown is not live proof. The move commits first, then both Orchs are woken best-effort: a failed wake
    never undoes or repeats the move. The result explicitly says committed, with notification failures
    recorded on the event row (or returned if recording itself failed). Each wake is serialized with
    further moves of those tasks; superseded acquisitions are omitted from the target's wake.
    """
    task_ids = list(dict.fromkeys(task_ids))
    if not task_ids and not from_orch:
        raise ValueError("name the task ids to adopt, or --from OLD_ORCH for all of its open tasks")
    target = store.get_orch(con, new_orch)
    if target is None:
        raise ValueError(f"unknown orch {new_orch}; the new owner must be registered")
    if target["stopped_at"] is not None:
        raise ValueError(f"orch {new_orch} is stopped")
    try:
        jobs = rt.live_jobs()
    except Exception:
        jobs = None
    target_health = owner_health(target, jobs)
    if target_health["state"] == "dead":
        raise ValueError(f"orch {new_orch} is dead ({target_health['reason']})")
    open_by_id = {t["id"]: t for t in store.open_tasks(con)}
    if task_ids:
        for tid in task_ids:
            if tid not in open_by_id:
                store.get_task(con, tid)  # KeyError for unknown ids
                raise ValueError(f"task {tid} is closed")
            if from_orch and open_by_id[tid]["orch_thread"] != from_orch:
                raise ValueError(f"task {tid} belongs to {open_by_id[tid]['orch_thread']}, not {from_orch}")
        tasks = [open_by_id[t] for t in task_ids]
    else:
        tasks = [t for t in open_by_id.values() if t["orch_thread"] == from_orch]
        if not tasks:
            raise ValueError(f"orch {from_orch} has no open tasks")
    for t in tasks:
        if t["orch_thread"] == new_orch:
            raise ValueError(f"task {t['id']} already belongs to {new_orch}")
    olds = {}
    old_rows = {}
    for t in tasks:
        owner = t["orch_thread"]
        if owner not in olds:
            old_rows[owner] = store.get_orch(con, owner)
            olds[owner] = owner_health(old_rows[owner], jobs)
    blocked = {o: h for o, h in olds.items() if h["state"] != "dead"}
    if blocked and not force:
        detail = ", ".join(f"{o} is {h['state']} ({h['reason']})" for o, h in blocked.items())
        raise ValueError(f"refusing to adopt: {detail}. Only a confirmed-dead owner moves without --force; "
                         "unknown is not proof the owner is gone")
    moves = store.AdoptionMoves(target, old_rows)
    for t in tasks:
        health = olds[t["orch_thread"]]
        body = f"adopted from {t['orch_thread']} ({health['state']}): {_adopt_summary(con, t)}"
        evidence = json.dumps(dict(from_orch=t["orch_thread"], to_orch=new_orch, forced=bool(force),
                                   recipient_orch=new_orch, old_owner_health=health), ensure_ascii=False)
        moves.append((t["id"], t["orch_thread"], body, evidence))
    message_ids = store.move_task_orch(con, moves, new_orch)
    old_notices = {}
    notification_errors = {}
    error_recording_failures = {}
    for owner, health in olds.items():
        if health["state"] == "dead":
            continue  # confirmed dead: nobody to tell
        owned = [t for t in tasks if t["orch_thread"] == owner]
        text = (f"[orchd] Nat moved {len(owned)} task(s) from you to {new_orch}: "
                + ", ".join(f"{t['repo']}/{t['id']}" for t in owned) + ". They are no longer yours.")
        mid = None
        try:
            with store.task_delivery(con, [t["id"] for t in owned]):
                # Removal notices describe this event, and make no current-ownership claim if the
                # old owner has since reacquired any of the tasks.
                if any(store.get_task(con, t["id"])["orch_thread"] == owner for t in owned):
                    text = text.replace("They are no longer yours.", "Historical transfer; check inbox for current ownership.")
                mid = store.add_message(con, owned[0]["id"], "adopt_notice", text,
                                        json.dumps(dict(old_orch=owner, to_orch=new_orch,
                                                        recipient_orch=owner, task_ids=[t["id"] for t in owned])))
                con.execute("UPDATE messages SET recipient_orch=? WHERE id=?", (owner, mid))
                _notify_orch(con, rt, owner, owned[0]["codex_bin"], text)
                old_notices[owner] = True
        except Exception as error:  # evidence stays queryable on the event row
            failure = f"{type(error).__name__}: {error}"[:500]
            notification_errors[owner] = failure
            try:
                if mid is not None:
                    con.execute("UPDATE messages SET wake_error=? WHERE id=?", (failure, mid))
                else:  # notice INSERT failed after the transfer committed: use its durable adopt events
                    con.executemany("UPDATE messages SET notice_error=?, notice_recipient=? WHERE id=?",
                                    [(failure, owner, mid) for t, mid in zip(tasks, message_ids)
                                     if t["orch_thread"] == owner])
            except Exception as recording_error:
                error_recording_failures[owner] = f"{type(recording_error).__name__}: {recording_error}"[:500]
            old_notices[owner] = False
    superseded = None
    current = tasks
    new_owner_error = None
    try:
        with store.task_delivery(con, [t["id"] for t in tasks]):
            current = [t for t in tasks if store.get_task(con, t["id"])["orch_thread"] == new_orch]
            superseded = [t["id"] for t in tasks if t not in current]
            # Never announce a stale acquisition. The committed events stay in history; only tasks
            # still owned by the target are included in its current acquisition wake.
            if current:
                text = (f"[orchd] adopted {len(current)} task(s) from "
                        + ", ".join(sorted({t["orch_thread"] for t in current})) + ": "
                        + ", ".join(f"{t['repo']}/{t['id']}" for t in current)
                        + " — 請呼叫 orchd 的 inbox 工具讀取。")
                _notify_orch(con, rt, new_orch, current[0]["codex_bin"], text)
            new_woken = bool(current)
    except Exception as error:
        new_woken = False
        new_owner_error = f"{type(error).__name__}: {error}"[:500]
        try:
            con.executemany("UPDATE messages SET wake_error=? WHERE id=?",
                            [(new_owner_error, mid) for t, mid in zip(tasks, message_ids) if t in current])
        except Exception as recording_error:
            error_recording_failures[new_orch] = f"{type(recording_error).__name__}: {recording_error}"[:500]
    return dict(committed=True, adopted=[t["id"] for t in tasks], to_orch=new_orch, forced=bool(force),
                superseded=superseded, notification_errors=notification_errors,
                new_owner_error=new_owner_error, error_recording_failures=error_recording_failures,
                from_orch={o: h["state"] for o, h in olds.items()},
                new_owner_health=target_health, new_owner_woken=new_woken, old_owner_notified=old_notices)
