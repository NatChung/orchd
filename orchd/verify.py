"""Hash-locked verify commands, rerun by an independent worker at the locked SHA (issue #4).

The Orch locks a task after its author reports: orchd records the author's HEAD, the git object hash of
each declared path at that HEAD, and the command. A different worker (a task dispatched with
`verifies=<author task>`) then runs `orchd verify <its task id>`: in its own worktree it checks out the
locked SHA, re-checks the hashes, runs the locked command and records the result on the author's task.
The task counts as verified only while the author's branch tip still equals the locked SHA.

Records are plain `messages` rows (kind verify_lock / verification), like answer_queued; no schema change.
Trust: every worker runs as the same local user and can write this DB, so the record is tamper-evident,
not tamperproof. The Orch cross-checks it against the verifier's own report.
"""
import json
import subprocess

from . import store

LOCK, RESULT = "verify_lock", "verification"
TAIL = 4000
SYMLINK, GITLINK = "120000", "160000"


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def _dirty(cwd):
    return _git(cwd, "status", "--porcelain", "--untracked-files=all").strip()


def _head(cwd):
    return _git(cwd, "rev-parse", "HEAD").strip()


def _entries(cwd, rev):
    """{path: (mode, type, object hash)} for every tree and blob in rev, read from git, not the filesystem."""
    out = {}
    for item in _git(cwd, "ls-tree", "-r", "-t", "-z", "--full-tree", rev).split("\0"):
        if item:
            meta, path = item.split("\t", 1)
            mode, kind, obj = meta.split()
            out[path] = (mode, kind, obj)
    return out


def check_paths(paths, entries):
    """Return {path: object hash}; refuse anything that is not a plain tracked file or directory in the repo."""
    if not isinstance(paths, list) or not paths:
        raise ValueError("lock_verify needs a non-empty list of paths")
    hashes = {}
    for path in paths:
        if not isinstance(path, str) or not path or path.startswith("/") or "\\" in path:
            raise ValueError(f"lock path {path!r} must be a relative path inside the repo")
        parts = path.split("/")
        if any(p in ("", ".", "..") for p in parts) or ".git" in parts:
            raise ValueError(f"lock path {path!r} must not contain empty, '.', '..' or .git components")
        for i in range(1, len(parts) + 1):
            entry = entries.get("/".join(parts[:i]))
            if entry is None:
                raise ValueError(f"lock path {path!r} is not tracked at the locked commit")
            if entry[0] in (SYMLINK, GITLINK):
                raise ValueError(f"lock path {path!r} goes through a symlink or submodule")
        mode, kind, obj = entries[path]
        if kind == "tree":
            inner = [p for p, e in entries.items() if p.startswith(path + "/") and e[0] in (SYMLINK, GITLINK)]
            if inner:
                raise ValueError(f"lock path {path!r} contains a symlink or submodule: {inner[0]}")
        hashes[path] = obj
    return hashes


def latest(con, task_id, kind):
    row = con.execute("SELECT id, evidence FROM messages WHERE task_id=? AND kind=? ORDER BY id DESC LIMIT 1",
                      (task_id, kind)).fetchone()
    return None if row is None else dict(json.loads(row["evidence"]), id=row["id"])


def lock(con, task_id, paths, command=None, orch_thread=None):
    """Orch: lock the author's current HEAD. Reads the author's worktree; never writes it."""
    task = store.get_task(con, task_id)
    if task["status"] == "closed":
        raise ValueError(f"task {task_id} is closed")
    if task["verifies"]:
        raise ValueError(f"task {task_id} is a verifier; lock the task it verifies ({task['verifies']})")
    command = command or task["verify"]
    if not isinstance(command, str) or not command.strip():
        raise ValueError("lock_verify needs a command (or a task dispatched with verify)")
    worktree = task["worktree"]
    if not worktree:
        raise ValueError(f"task {task_id} has no worktree")
    if _dirty(worktree):
        raise ValueError(f"task {task_id}'s worktree has uncommitted or untracked files; lock only committed work")
    sha = _head(worktree)
    hashes = check_paths(list(dict.fromkeys(paths)) if isinstance(paths, list) else paths, _entries(worktree, sha))
    previous = latest(con, task_id, LOCK)
    record = dict(sha=sha, paths=hashes, command=command, orch_thread=orch_thread)
    mid = store.add_message(con, task_id, LOCK, f"locked {sha[:12]}: {command}", json.dumps(record))
    changed = []
    if previous:
        changed = [p for p, h in hashes.items() if p in previous["paths"] and previous["paths"][p] != h]
    return dict(task_id=task_id, lock_id=mid, sha=sha, paths=hashes, command=command, changed_paths=changed,
                command_changed=bool(previous) and previous["command"] != command)


def run(con, verifier_id, timeout=None):
    """Verifier worker: rerun the author's locked command at the locked SHA in the verifier's own worktree."""
    v = store.get_task(con, verifier_id)
    author_id = v["verifies"]
    if not author_id:
        raise ValueError(f"task {verifier_id} was not dispatched to verify another task")
    if author_id == verifier_id:
        raise ValueError("a task cannot verify itself")
    if v["status"] == "closed":
        raise ValueError(f"task {verifier_id} is closed")
    a = store.get_task(con, author_id)
    if a["status"] == "closed":
        raise ValueError(f"author task {author_id} is closed")
    if v["session_id"] and v["session_id"] == a["session_id"]:
        raise ValueError("verifier and author are the same worker session")
    locked = latest(con, author_id, LOCK)
    if locked is None:
        raise ValueError(f"author task {author_id} has no lock; the Orch must call lock_verify first")
    worktree = v["worktree"]
    if not worktree:
        raise ValueError(f"task {verifier_id} has no worktree")
    if _dirty(worktree):
        raise ValueError(f"task {verifier_id}'s worktree is not clean; commit or remove changes before verify")
    back = subprocess.run(["git", "symbolic-ref", "-q", "--short", "HEAD"], cwd=worktree,
                          capture_output=True, text=True).stdout.strip() or _head(worktree)
    sha = locked["sha"]
    _git(worktree, "checkout", "-q", "--detach", sha)
    head = _head(worktree)
    entries = _entries(worktree, head)
    try:
        hash_ok = head == sha and check_paths(list(locked["paths"]), entries) == locked["paths"]
    except ValueError:
        hash_ok = False
    try:
        done = subprocess.run(locked["command"], shell=True, cwd=worktree, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True, errors="replace", timeout=timeout)
        code, output = done.returncode, done.stdout
    except subprocess.TimeoutExpired as expired:
        out = expired.stdout or ""
        code, output = None, (out.decode(errors="replace") if isinstance(out, bytes) else out) + "\n[orchd] timed out"
    dirty = bool(_dirty(worktree))
    head_after = _head(worktree)
    restored = False
    if not dirty:  # leave a dirty tree where it is, so nothing the command wrote is thrown away
        restored = subprocess.run(["git", "checkout", "-q", back], cwd=worktree, capture_output=True).returncode == 0
    record = dict(verifier=verifier_id, verifier_session=v["session_id"], author=author_id, lock_id=locked["id"],
                  sha=head, command=locked["command"], exit=code, tail=output[-TAIL:], hash_ok=hash_ok,
                  dirty=dirty, head_after=head_after)
    record["passed"] = _passed(record, locked)
    store.add_message(con, author_id, RESULT,
                      f"{'pass' if record['passed'] else 'fail'} by {verifier_id} at {head[:12]} (exit {code})",
                      json.dumps(record))
    return dict(record, restored=restored)


def _passed(r, locked):
    return (r["exit"] == 0 and r["hash_ok"] is True and not r["dirty"] and r["sha"] == locked["sha"]
            and r["head_after"] == locked["sha"] and r["lock_id"] == locked["id"] and r["verifier"] != r["author"])


def _current_sha(task):
    """The author's branch tip; the shared branch ref outlives the worktree."""
    candidates = [(task["repo_path"], f"refs/heads/{task['branch']}")] if task["branch"] else []
    for cwd, rev in candidates + [(task["worktree"], "HEAD")]:
        if cwd:
            got = subprocess.run(["git", "rev-parse", "--verify", "-q", rev], cwd=cwd, capture_output=True, text=True)
            if got.returncode == 0:
                return got.stdout.strip()
    return None


def status(con, task_id):
    """state: none (no lock) | locked (no independent run yet) | pass | fail | stale (author moved past the lock).

    Only verification rows written by a different task count; the author's report never does."""
    task = store.get_task(con, task_id)
    role = "verifier" if task["verifies"] else "author"
    author = store.get_task(con, task["verifies"]) if task["verifies"] else task
    locked = latest(con, author["id"], LOCK)
    if locked is None:
        return dict(state="none", role=role, author=author["id"])
    rows = con.execute("SELECT evidence FROM messages WHERE task_id=? AND kind=? ORDER BY id DESC",
                       (author["id"], RESULT)).fetchall()
    result = next((r for r in (json.loads(row["evidence"]) for row in rows) if r["lock_id"] == locked["id"]), None)
    current = _current_sha(author)
    out = dict(role=role, author=author["id"], verifier=result and result["verifier"], lock_sha=locked["sha"],
               verified_sha=result and result["sha"], current_sha=current, command=locked["command"])
    if current != locked["sha"]:
        return dict(out, state="stale")
    if result is None:
        return dict(out, state="locked")
    out.update(exit=result["exit"], hash_ok=result["hash_ok"], dirty=result["dirty"])
    return dict(out, state="pass" if _passed(result, locked) else "fail")


def has_any(con, task):
    """Cheap DB-only check so tasks with nothing to verify never shell out to git."""
    if task["verifies"]:
        return True
    return con.execute("SELECT 1 FROM messages WHERE task_id=? AND kind=? LIMIT 1", (task["id"], LOCK)).fetchone() is not None
