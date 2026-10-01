"""Task operations shared by the Orch MCP tools and the worker CLI."""
import json
import os
import shlex
import uuid
from pathlib import Path

from . import store, worker_health
from .orch_health import owner_health
from .runtime import DEFAULT_ORCH_MODEL, DEFAULT_WORKER_MODEL, MODELS, claude_job_alive, worker_kind

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
    text = wake_text(task, line)
    orch = store.get_orch(con, task["orch_thread"])
    try:
        if orch is not None and orch["kind"] == "claude":
            rt.send_uds(orch["socket"], orch["session_id"], text)
        else:  # Codex Orch, including tasks dispatched before orchs were registered
            rt.wake_orch(task["codex_bin"], task["orch_thread"], text)
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


def close(con, rt, task_id, outcome=None, rating=None):
    """Stop the worker; remove the worktree only when nothing local would be lost."""
    task = store.get_task(con, task_id)
    if task["status"] == "closed":
        return dict(task_id=task_id, closed=True, worktree="already closed")
    if outcome is not None:
        _choice("outcome", outcome, OUTCOMES)
    if rating is not None and (isinstance(rating, bool) or not isinstance(rating, int) or not 1 <= rating <= 3):
        raise ValueError("rating must be an integer 1-3")
    store.add_message(con, task_id, "close", json.dumps(dict(outcome=outcome, rating=rating)))
    store.update_task(con, task_id, outcome=outcome, rating=rating)
    if task["job_id"]:
        (rt.stop_codex if worker_kind(task["model"]) == "codex" else rt.stop_worker)(task["job_id"])
    kept = None
    if task["worktree"]:
        removable, reason = rt.worktree_state(task["worktree"], task["base"])
        if removable:
            rt.remove_worktree(task["repo_path"], task["worktree"])
        else:
            kept = reason
    _record_usage(con, rt, task)
    store.update_task(con, task_id, status="closed",
                      note=(f"worktree kept: {kept}" if kept else task["note"]))
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
