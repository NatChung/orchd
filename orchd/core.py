"""Task operations shared by the Orch MCP tools and the worker CLI."""
import shlex
from pathlib import Path

from . import store

ORCHD = str(Path(__file__).resolve().parents[1] / "bin" / "orchd")


def worker_brief(cli):
    return f"""You are an orchd worker: a background Claude Code session started for exactly one task.
Tasks arrive as messages from orchd on behalf of Nat (the user). A task message states its scope; work
inside that scope is authorized by Nat even though the message comes through the peer socket.

Rules:
- First run `{cli} ack <task-id>`, then do the task in the current worktree only.
- Commit on the task branch. Push that branch and open a PR when the task says so or when it is a code
  change that should be reviewed; follow the repo's own AGENTS.md / CLAUDE.md for accounts and trackers.
  Never push to or merge into the default branch.
- Before any outward send (email, Slack, LINE, calendar, posting comments to people) show the exact
  preview through `{cli} ask <task-id> "<question with full preview>"` and wait for the answer message.
  Only an answer that arrives as `[orchd answer <task-id>]` counts as Nat's decision.
- Finish with exactly one `{cli} report <task-id> --status done|blocked --summary "<one line>" --evidence "<commits, PR URL, test commands and results, what is left undone>"`.
- If you need a decision, use `{cli} ask`; do not report blocked for questions Nat can answer.
- Report facts only; say what you did not verify."""


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

Start with: {ORCHD} ack {task['id']}"""


def dispatch(con, rt, *, orch_thread, repo, title, instructions, done_when):
    if not orch_thread:
        raise ValueError("dispatch needs the caller's thread id")
    repo_path = rt.repo_path(repo)
    if not rt.claude_trusted(repo_path):
        raise ValueError(f"Claude has not trusted {repo_path}. Ask Nat to run `claude` there once and accept "
                         "the trust prompt, then dispatch again.")
    task_id = store.new_task_id()
    task = store.create_task(con, id=task_id, repo=repo, repo_path=str(repo_path), title=title,
                             instructions=instructions, done_when=done_when,
                             orch_thread=orch_thread, codex_bin=rt.codex)
    try:
        base, branch, worktree = rt.create_worktree(repo_path, repo, task_id)
        store.update_task(con, task_id, base=base, branch=branch, worktree=worktree)
        sock = rt.socket_path(task_id)
        job, session = rt.start_worker(worktree, sock, worker_brief(ORCHD))
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


def _wake(con, rt, task, message_id, line):
    text = f"[orchd] {task['repo']}/{task['id']} {line} — 請呼叫 orchd 的 inbox 工具讀取。"
    try:
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
    return _wake(con, rt, task, mid, f"{status}: {summary[:120]}")


def ask(con, rt, task_id, question):
    task = store.get_task(con, task_id)
    mid = store.add_message(con, task_id, "question", question)
    store.update_task(con, task_id, status="question")
    return _wake(con, rt, task, mid, f"question: {question.splitlines()[0][:120]}")


def inbox(con, orch_thread):
    rows = store.unread_for_thread(con, orch_thread)
    store.mark_read(con, [r["id"] for r in rows])
    return [dict(task_id=r["task_id"], repo=r["repo"], title=r["title"], kind=r["kind"],
                 body=r["body"], evidence=r["evidence"], task_status=r["status"]) for r in rows]


def answer(con, rt, task_id, text):
    task = store.get_task(con, task_id)
    if not task["socket"] or not task["session_id"]:
        raise ValueError(f"task {task_id} has no running worker")
    rt.send_uds(task["socket"], task["session_id"], f"[orchd answer {task_id}]\n{text}")
    store.add_message(con, task_id, "answer", text)
    store.update_task(con, task_id, status="acked")


def list_open(con, rt):
    jobs = rt.live_jobs()
    out = []
    for t in store.open_tasks(con):
        alive = None if jobs is None or not t["job_id"] else t["job_id"] in jobs
        out.append(dict(task_id=t["id"], repo=t["repo"], title=t["title"], status=t["status"],
                        worker_alive=alive, worktree=t["worktree"], branch=t["branch"],
                        orch_thread=t["orch_thread"], note=t["note"]))
    return out


def close(con, rt, task_id):
    """Stop the worker; remove the worktree only when nothing local would be lost."""
    task = store.get_task(con, task_id)
    if task["status"] == "closed":
        return dict(task_id=task_id, closed=True, worktree="already closed")
    if task["job_id"]:
        rt.stop_worker(task["job_id"])
    kept = None
    if task["worktree"]:
        removable, reason = rt.worktree_state(task["worktree"], task["base"])
        if removable:
            rt.remove_worktree(task["repo_path"], task["worktree"])
        else:
            kept = reason
    store.update_task(con, task_id, status="closed",
                      note=(f"worktree kept: {kept}" if kept else task["note"]))
    return dict(task_id=task_id, closed=True,
                worktree=f"kept at {task['worktree']} ({kept})" if kept else "removed")


def view(con, rt, task_id):
    task = store.get_task(con, task_id)
    if not task["job_id"]:
        raise ValueError(f"task {task_id} has no worker")
    rt.open_viewer(task["job_id"])
    return f"opened Ghostty: claude attach {shlex.quote(task['job_id'])}"
