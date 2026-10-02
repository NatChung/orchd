"""Hash-locked verify commands, rerun by an independent worker at the locked SHA (issue #4).

The Orch locks a task after its author reports: orchd records the author's HEAD, the git object hash of
each declared path at that HEAD, and the command. A different worker (a task dispatched with
`verifies=<author task>`) then runs `orchd verify <its task id>`: in its own worktree it checks out the
locked SHA, re-checks the hashes, runs the locked command and records the result on the author's task.
The task counts as verified only while the author's branch tip still equals the locked SHA.

Records are plain `messages` rows (kind verify_lock / verification), like answer_queued; no schema change.
Trust: every worker runs as the same local user and can write this DB, so the record is tamper-evident,
not tamperproof. The Orch cross-checks it against the verifier's own report.

The locked command runs as its own process group; whatever is left of that group is killed and confirmed gone
before the tree is judged or restored. A child that leaves the group (setsid, setpgid, a daemon) is not caught.
Supported: POSIX (macOS, Linux) with Python 3.9+; anything else is refused before the checkout.
"""
import json
import os
import select
import signal
import subprocess
import sys
import tempfile
import time

from . import store

LOCK, RESULT = "verify_lock", "verification"
TAIL = 4000
CLEANUP_WAIT = 5.0  # seconds to see the command's process group empty after SIGKILL
SUPPORTED = "POSIX (macOS, Linux) with Python 3.9 or newer"
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
    missing = unsupported()
    if missing:
        raise ValueError(f"this host can't run a locked command with confirmed cleanup (missing: {', '.join(missing)}); "
                         f"verify needs {SUPPORTED}")
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
    record = dict(verifier=verifier_id, verifier_session=v["session_id"], author=author_id, lock_id=locked["id"],
                  sha=None, command=locked["command"], exit=None, tail="", hash_ok=False, dirty=None,
                  head_after=None, cleanup=None, restored=False, restore_error=None)
    restorable = True  # until a command starts whose processes are not confirmed gone
    try:
        head = record["sha"] = _head(worktree)
        try:
            record["hash_ok"] = head == sha and check_paths(list(locked["paths"]), _entries(worktree, head)) == locked["paths"]
        except ValueError:
            pass
        restorable = False
        code, output, cleanup = _execute(locked["command"], worktree, timeout)
        record.update(exit=code, tail=output[-TAIL:], cleanup=cleanup)
        restorable = cleanup
        if cleanup:  # nothing of the command is left to write, so the tree can be judged
            record["dirty"] = bool(_dirty(worktree))
            record["head_after"] = _head(worktree)
    except BaseException as error:
        record["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        if restorable:
            record["restored"], record["restore_error"] = _restore(worktree, back)
        else:
            record["restore_error"] = "not attempted: cleanup of the command's processes not confirmed"
        record["passed"] = _passed(record, locked)
        store.add_message(con, author_id, RESULT,
                          f"{'pass' if record['passed'] else 'fail'} by {verifier_id} at "
                          f"{(record['sha'] or '?')[:12]} (exit {record['exit']})", json.dumps(record))
    return record


def unsupported():
    """What this host lacks for _execute; checked before the checkout so a missing piece refuses instead of crashing."""
    missing = [] if os.name == "posix" else [f"POSIX process groups (os.name is {os.name!r})"]
    missing += [f"os.{name}" for name in ("killpg", "setsid", "pipe") if not hasattr(os, name)]
    missing += [] if hasattr(select, "select") else ["select.select"]
    missing += [] if sys.executable and os.access(sys.executable, os.X_OK) else ["an executable sys.executable"]
    missing += [] if os.access("/bin/sh", os.X_OK) else ["/bin/sh"]
    return missing


# The holder leads the command's process group: it runs the command as its child, reaps it, reports the exit
# status, then blocks on stdin. While it lives (or is an unreaped zombie of orchd) its pid, the group id, can't
# be reused, so orchd's SIGKILL to the group can't reach an unrelated process. Plain Python, no os.waitid.
_HOLDER = """import os, subprocess, sys
code = subprocess.call(["/bin/sh", "-c", sys.argv[1]], stdin=subprocess.DEVNULL)
os.write(int(sys.argv[2]), b"%d\\n" % code)
sys.stdin.buffer.read()
"""


def _execute(command, cwd, timeout):
    """Run command under a holder in a new session, then kill what is left of its group. Returns (exit, output, cleanup).

    SIGKILL goes to the group before orchd lets the holder go and reaps it; afterwards the group is only probed
    with signal 0. cleanup is True once the group is empty. Output goes to a file: a background child holding a
    pipe would stall the read."""
    with tempfile.TemporaryFile() as out:
        status_r, status_w = os.pipe()
        try:
            try:
                proc = subprocess.Popen([sys.executable, "-I", "-c", _HOLDER, command, str(status_w)], cwd=cwd,
                                        stdin=subprocess.PIPE, stdout=out, stderr=subprocess.STDOUT,
                                        start_new_session=True, pass_fds=(status_w,))
            finally:
                os.close(status_w)
            try:
                exited, code = _read_status(status_r, timeout)
            finally:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass  # the holder is already gone; _group_gone decides
                proc.stdin.close()  # lets a holder that was not killed exit
                proc.wait()
        finally:
            os.close(status_r)
        cleanup = _group_gone(proc.pid)
        out.seek(0)
        output = out.read().decode(errors="replace")
    if not exited:
        return None, output + "\n[orchd] timed out", cleanup
    if code is None:
        return None, output + "\n[orchd] the command's holder ended without an exit status", cleanup
    return code, output, cleanup


def _read_status(fd, timeout):
    """(exited, exit status) from the holder's status pipe; (False, None) on timeout, (True, None) if it closed early."""
    deadline = None if timeout is None else time.monotonic() + timeout
    data = b""
    while not data.endswith(b"\n"):
        wait = None if deadline is None else max(0.0, deadline - time.monotonic())
        if not select.select([fd], [], [], wait)[0]:
            return False, None
        chunk = os.read(fd, 64)
        if not chunk:
            return True, None
        data += chunk
    return True, int(data)


def _group_gone(pgid):
    deadline = time.monotonic() + CLEANUP_WAIT
    while True:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            pass  # a killed child not yet reaped by init, or a process orchd can't signal: not gone either way
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)


def _restore(worktree, back):
    """Switch back without -f, so git refuses rather than overwrite. A dirty tree stays where it is."""
    try:
        if _dirty(worktree):
            return False, None
    except (subprocess.CalledProcessError, OSError) as error:
        return False, f"could not check the tree, left as is: {error}"
    got = subprocess.run(["git", "checkout", "-q", back], cwd=worktree, capture_output=True, text=True)
    return (True, None) if got.returncode == 0 else (False, got.stderr.strip() or f"git checkout exit {got.returncode}")


def _passed(r, locked):
    return (r["exit"] == 0 and r["hash_ok"] is True and r.get("cleanup") is True and r["dirty"] is False
            and not r.get("error") and r["sha"] == locked["sha"]
            and r["head_after"] == locked["sha"] and r["lock_id"] == locked["id"] and r["verifier"] != r["author"])


def _current_sha(task):
    """The author's branch tip; the shared branch ref outlives the worktree."""
    candidates = [(task["repo_path"], f"refs/heads/{task['branch']}")] if task["branch"] else []
    for cwd, rev in candidates + [(task["worktree"], "HEAD")]:
        if cwd and os.path.isdir(cwd):
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
