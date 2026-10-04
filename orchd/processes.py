"""Task-scoped process cleanup, independent of vendor jobs and POSIX groups.

ORCHD_WORKTREE is inherited at exec, so nohup/setsid/reparenting and chdir do
not lose ownership. Never infer ownership from a process name or cwd alone.
psutil's Process objects retain (pid, create_time) and check reuse on signals.
"""
import os
import time
from pathlib import Path

import psutil


TERM_GRACE = 2.0
KILL_GRACE = 2.0


def _descendants(proc, mark, found):
    """Do not cross into another task, even if a worker dispatched that task."""
    for child in proc.children():
        try:
            owner = child.environ().get("ORCHD_WORKTREE")
            if owner and owner != mark:
                continue
            child.create_time()
            if child.pid != os.getpid():
                found[child.pid] = child
                _descendants(child, mark, found)
        except psutil.NoSuchProcess:
            continue


def task_processes(worktree, captured=()):
    """Snapshot marked processes and their descendants before the worker exits."""
    mark = str(Path(worktree).resolve())
    found = {}
    for proc in captured:
        if _alive(proc):
            _descendants(proc, mark, found)
    for proc in psutil.process_iter():
        try:
            if proc.pid == os.getpid() or proc.uids().real != os.getuid():
                continue
            if proc.environ().get("ORCHD_WORKTREE") != mark:
                continue
            # Force identity acquisition now, before a PID can disappear/reappear.
            proc.create_time()
            found[proc.pid] = proc
            _descendants(proc, mark, found)
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
        except psutil.AccessDenied:
            if proc.pid in found:
                raise RuntimeError(f"cannot inspect descendants of task process {proc.pid}; worktree kept") from None
            # Protected unrelated sessions cannot be inspected. A task-local
            # process whose ownership is unreadable blocks close, never gets killed.
            try:
                if Path(proc.cwd()).resolve().is_relative_to(Path(mark)):
                    raise RuntimeError(f"cannot inspect task-local process {proc.pid}; worktree kept")
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    return found


def _alive(proc):
    try:
        return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def cleanup_processes(worktree, captured):
    """TERM, bounded grace, KILL; rescan for late children and confirm exit.
    Signal only captured Process identities. Return actual signalled/exited PIDs.
    A failed cleanup raises so close retains the task and worktree for retry.
    """
    if not worktree:
        return None
    known = dict(captured)
    term, killed = set(), set()
    last_term = None
    deadline = time.monotonic() + TERM_GRACE
    try:
        while True:
            known.update(task_processes(worktree, list(known.values())))
            live = [p for p in known.values() if _alive(p)]
            for proc in live:
                if proc.pid not in term:
                    try:
                        proc.terminate()
                        term.add(proc.pid)
                        last_term = time.monotonic()
                    except psutil.NoSuchProcess:
                        pass
            if not live or time.monotonic() >= deadline:
                break
            time.sleep(.05)
        # A child discovered near the deadline gets its own full TERM grace.
        if last_term is not None:
            while any(_alive(p) for p in known.values()) and time.monotonic() < last_term + TERM_GRACE:
                time.sleep(.05)
        for proc in known.values():
            if _alive(proc):
                try:
                    proc.kill()
                    killed.add(proc.pid)
                except psutil.NoSuchProcess:
                    pass
        deadline = time.monotonic() + KILL_GRACE
        while any(_alive(p) for p in known.values()) and time.monotonic() < deadline:
            time.sleep(.05)
        # Never silently close if a final late-spawned marked process escaped.
        known.update(task_processes(worktree))
        remaining = sorted(p.pid for p in known.values() if _alive(p))
        if remaining:
            raise RuntimeError(f"task processes still alive after SIGKILL: {remaining}; "
                               f"SIGTERM PIDs {sorted(term)}, SIGKILL PIDs {sorted(killed)}")
    except psutil.AccessDenied as e:
        raise RuntimeError(f"cannot stop task process {e.pid}; SIGTERM PIDs {sorted(term)}, "
                           f"SIGKILL PIDs {sorted(killed)}") from None
    if not known:
        return None
    return {"terminated": sorted(term), "killed": sorted(killed),
            "exited": sorted(p.pid for p in known.values() if not _alive(p))}
