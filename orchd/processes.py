"""Task-scoped process cleanup, independent of vendor jobs and POSIX groups.

ORCHD_WORKTREE is inherited at exec, so nohup/setsid/reparenting and chdir do
not lose ownership. Never infer ownership from a process name or cwd alone.
psutil's Process objects retain (pid, create_time) and check reuse on signals.
"""
import os
import subprocess
import sys
import time
from pathlib import Path

import psutil


TERM_GRACE = 2.0
KILL_GRACE = 2.0


def environ_of(proc, attempts=4, *, exclude_unreadable=None):
    """A process mid-exec briefly refuses its environment, and one that exits mid-read makes psutil's C code raise
    SystemError ("returned a result with an exception set"). Retry; a failure that persists is NoSuchProcess when
    the process is gone and AccessDenied (unknown, blocks) when it is still there. Ownership scans may exclude
    independently proven service infrastructure after the first AccessDenied, before sleeping or retrying."""
    for attempt in range(attempts):
        try:
            return proc.environ()
        except (psutil.AccessDenied, SystemError) as error:
            if (attempt == 0 and isinstance(error, psutil.AccessDenied)
                    and exclude_unreadable is not None and exclude_unreadable(proc)):
                return {}
            if attempt < attempts - 1:
                time.sleep(.05)
    if not _alive(proc):
        raise psutil.NoSuchProcess(proc.pid)
    raise psutil.AccessDenied(proc.pid)


def _linux_process_stat(pid):
    # comm may contain spaces and parentheses. Remaining fields begin at state.
    raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    fields = raw.rsplit(")", 1)[1].split()
    return dict(ppid=int(fields[1]), group=int(fields[2]), session=int(fields[3]),
                flags=int(fields[6]), start=int(fields[19]))


def _user_manager_unit(uid):
    command = Path("/usr/bin/systemctl")
    info = command.stat()
    if info.st_uid != 0 or info.st_mode & 0o022:
        raise ValueError("systemctl is not a trusted system executable")
    properties = ("MainPID", "Type", "PAMName", "ActiveState", "ControlGroup", "ActiveEnterTimestampMonotonic")
    result = subprocess.run([str(command), "--system", "show", f"user@{uid}.service",
                             *["--property=" + name for name in properties]], capture_output=True,
                            text=True, check=True, timeout=2,
                            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C", "SYSTEMD_PAGER": ""})
    return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)


def _systemd_user_infrastructure(proc):
    """Exclude only a root-managed user manager and its pre-READY, never-execed PAM helper.

    Their environments can be unreadable despite matching UIDs. Names and writable
    user cgroups alone cannot establish this exemption. The system manager supplies
    the authoritative MainPID and service-ready time; procfs supplies kernel lineage
    and PF_FORKNOEXEC. Missing evidence leaves the ordinary fail-closed scan intact.
    """
    if not sys.platform.startswith("linux"):
        return False
    try:
        name = proc.name()
        if name not in ("systemd", "(sd-pam)"):
            return False
        uid = os.getuid()
        unit = _user_manager_unit(uid)
        if (unit.get("Type") not in ("notify", "notify-reload") or unit.get("PAMName") != "systemd-user"
                or unit.get("ActiveState") not in ("active", "reloading")):
            return False
        manager = psutil.Process(int(unit["MainPID"]))
        manager_stat = _linux_process_stat(manager.pid)
        current = _linux_process_stat(proc.pid)
        ready = int(unit["ActiveEnterTimestampMonotonic"]) / 1_000_000
        ticks = os.sysconf("SC_CLK_TCK")
        # A tick is a rounded-down birth timestamp. Require its upper bound to
        # precede READY so a later process cannot pass through timestamp rounding.
        if ready <= 0 or (current["start"] + 1) / ticks > ready:
            return False
        if manager_stat["ppid"] != 1 or manager.name() != "systemd" or "--user" not in manager.cmdline():
            return False
        if any(value != uid for value in manager.uids()):
            return False
        scope = unit["ControlGroup"].rstrip("/") + "/init.scope"
        if scope != f"/user.slice/user-{uid}.slice/user@{uid}.service/init.scope":
            return False
        for pid in {proc.pid, manager.pid}:
            groups = Path(f"/proc/{pid}/cgroup").read_text(encoding="utf-8").splitlines()
            if not any(line.split(":", 2)[-1] == scope for line in groups):
                return False
        if proc.pid == manager.pid:
            return name == "systemd" and proc.is_running()
        return (name == "(sd-pam)" and current["ppid"] == manager.pid
                and current["flags"] & 0x40 != 0  # Linux PF_FORKNOEXEC: forked but never execed
                and current["group"] == manager_stat["group"]
                and current["session"] == manager_stat["session"]
                and proc.is_running() and manager.is_running())
    except (OSError, ValueError, KeyError, IndexError, psutil.Error, subprocess.SubprocessError):
        return False


def _descendants(proc, mark, found, exclude):
    """Do not cross into another task, even if a worker dispatched that task."""
    try:
        children = proc.children()
    except psutil.NoSuchProcess:  # already gone: nothing of it left to track
        return
    for child in children:
        try:
            # Lineage and an inherited mark do not authorize signalling another
            # account. Apply the same owner check as the process-wide scan.
            if not _marked_task_process(child, mark, exclude):
                continue
            child.create_time()
            if child.pid != os.getpid():
                found[child.pid] = child
                _descendants(child, mark, found, exclude)
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied:
            raise RuntimeError(f"cannot inspect process {child.pid}; worktree kept") from None


_WINDOWS_USER = None


def _owned_by_us(proc):
    """Compare real uids or Windows account names; report unknown owners without signalling them."""
    if os.name != "nt":
        uids = proc.uids()
        uid = os.getuid()
        return uids.real == uid and uids.effective == uid
    global _WINDOWS_USER
    if _WINDOWS_USER is None:
        _WINDOWS_USER = psutil.Process().username()
    return proc.username() == _WINDOWS_USER


def _predates_worktree(mark):
    """Windows shell/IME helpers of this user (ctfmon, taskhostw, ...) run at a higher integrity level and refuse
    environment reads. The mark is set only on processes started after the worktree was created, so a process
    born before that cannot carry it. When the worktree is already gone there is nothing left to protect from
    removal, and this user could not signal a higher-integrity process anyway, so unreadable ones are skipped."""
    try:
        born = os.stat(mark).st_ctime  # creation time on Windows
    except (FileNotFoundError, NotADirectoryError):
        return lambda proc: True
    return lambda proc: proc.create_time() < born


def _started_by_another_account(proc):
    """Windows: a process gets its environment from whoever creates it, so one created by another account's
    process (a system service starting ctfmon, ChtIME, taskhostw, a search or WMI host, ...) cannot carry the mark.
    After a sign-in these start later than every older worktree, refuse environment reads, and used to block close.
    Only a parent whose owner is positively read as another account counts as evidence. UAC/AIS launches name the
    requesting process as parent, so an elevated child of a worker keeps a parent of ours and still blocks. A gone
    parent (a worker may start an elevated child that inherits the mark and then exit) or a parent whose owner
    cannot be read is unknown, never evidence: the process stays unknown and blocks close. psutil's parent()
    rejects a parent PID reused after the child started."""
    try:
        parent = proc.parent()
        return parent is not None and not _owned_by_us(parent)
    except psutil.Error:  # gone, access denied or otherwise unreadable: no independent evidence
        return False


def _windows_sessions():
    """{pid: session id} from one WTSEnumerateProcessesW snapshot, or None. It needs no handle to each process,
    so it also reads services and protected processes (csrss, search hosts) this user cannot open."""
    import ctypes
    from ctypes import wintypes

    class Info(ctypes.Structure):
        _fields_ = [("SessionId", wintypes.DWORD), ("ProcessId", wintypes.DWORD),
                    ("pProcessName", wintypes.LPWSTR), ("pUserSid", ctypes.c_void_p)]
    try:
        wts = ctypes.WinDLL("wtsapi32", use_last_error=True)
        wts.WTSEnumerateProcessesW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                               ctypes.POINTER(ctypes.POINTER(Info)), ctypes.POINTER(wintypes.DWORD)]
        wts.WTSEnumerateProcessesW.restype = wintypes.BOOL
        wts.WTSFreeMemory.argtypes = [ctypes.c_void_p]
        info, count = ctypes.POINTER(Info)(), wintypes.DWORD()
        if not wts.WTSEnumerateProcessesW(None, 0, 1, ctypes.byref(info), ctypes.byref(count)):
            return None
        try:
            return {info[i].ProcessId: info[i].SessionId for i in range(count.value)}
        finally:
            wts.WTSFreeMemory(info)
    except (OSError, AttributeError):
        return None


def _service_session(proc):
    """Windows: session 0 holds only services and what they start. A worker of this user, elevated or not,
    stays in the interactive session and cannot create a session-0 process, so an unreadable session-0 process
    cannot carry the mark. Without this, every Windows service started after a worktree (after a reboot: all of
    them) blocked close. The snapshot is read after `proc` exists and `proc` must still be running afterwards
    (psutil checks its birth), so a PID reused meanwhile is never judged by another process's session. An
    unknown session, or orchd itself running in session 0, is no evidence."""
    sessions = _windows_sessions()
    if not sessions or sessions.get(os.getpid()) in (None, 0) or sessions.get(proc.pid) != 0:
        return False
    try:
        return proc.is_running()
    except psutil.Error:
        return False


def _marked_task_process(proc, mark, exclude):
    """Apply one ownership gate to enumerated processes and newly found children."""
    try:
        owned = _owned_by_us(proc)
    except psutil.AccessDenied:
        # Unknown owners are never signalled. Exclude only with independent evidence.
        if os.name == "nt":
            def unknown_unreadable(p):
                return exclude(p) or _started_by_another_account(p)
            if environ_of(proc, exclude_unreadable=unknown_unreadable).get("ORCHD_WORKTREE") != mark:
                return False
        raise
    if os.name == "nt":
        def own_unreadable(p):
            return exclude(p) or _started_by_another_account(p)
        return owned and environ_of(proc, exclude_unreadable=own_unreadable).get("ORCHD_WORKTREE") == mark
    return owned and environ_of(proc, exclude_unreadable=exclude).get("ORCHD_WORKTREE") == mark


def task_processes(worktree, captured=()):
    """Snapshot marked processes and their descendants before the worker exits."""
    mark = str(Path(worktree).resolve())
    found = {}
    if os.name == "nt":
        predates = _predates_worktree(mark)

        def exclude(proc):
            return predates(proc) or _service_session(proc)
    else:
        exclude = _systemd_user_infrastructure
    for proc in captured:
        if _alive(proc):
            _descendants(proc, mark, found, exclude)
    for proc in psutil.process_iter():
        try:
            if proc.pid == os.getpid():
                continue
            if not _marked_task_process(proc, mark, exclude):
                continue
            # Force identity acquisition now, before a PID can disappear/reappear.
            proc.create_time()
            found[proc.pid] = proc
            _descendants(proc, mark, found, exclude)
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
        except SystemError:  # psutil's C code lost a race with the process exiting
            if not proc.is_running():
                continue
            raise RuntimeError(f"cannot inspect process {proc.pid}; worktree kept") from None
        except psutil.AccessDenied:
            # The mark is the only proof of ownership. A process of ours whose mark cannot be read is unknown,
            # not unrelated: it blocks close (worktree kept) and is never signalled.
            raise RuntimeError(f"cannot inspect process {proc.pid}; worktree kept") from None
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
