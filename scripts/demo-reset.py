#!/usr/bin/env python3
"""Reset the tech-sharing demo (demo-shop-api / -web / -docs) after a run.

Dry-run by default: prints what it would remove and changes nothing. `--apply` removes it.

Only resources that the orchd DB attributes to a *closed* demo-shop task are eligible: the task's
worktree, its local `orchd/<id>` branch, the same branch on the demo remote, and its /tmp socket dir.
Paths are rebuilt from the task id, never taken from the DB or a glob. Anything that could lose data
(dirty/untracked/unpushed work, submodules, a live socket, a symlink, a path or remote that is not
exactly where the demo puts it) is refused with the reason; the rest still proceeds.
Nothing here forces git: no --force, no branch -D, no repo-wide prune.

Which ledger is trusted:
- Dry-run reads a private copy of the DB (+WAL) and never opens the live file, so it creates no
  sidecars. It is a snapshot: a writer may change the DB right after; the dry-run is advisory.
- --apply first takes the same snapshot (an open demo task there stops it without touching the live
  DB), then opens the live DB and holds SQLite's write lock (BEGIN IMMEDIATE ... ROLLBACK) from the
  authoritative re-read through the last removal. No writer can open or reopen a task while it holds
  the lock. It writes no rows; opening the live DB does create -wal/-shm for the duration, and if it is
  the last connection on close SQLite checkpoints a WAL left by others into orchd.db (content unchanged).
  orchd writers wait up to 30s (store.connect busy timeout): no new task chain starts after
  LOCK_BUDGET_S, so the hold is that plus the chain already in flight (up to ~7 git calls, each
  capped at GIT_TIMEOUT_S); keep demo runs small enough for that to stay under 30s. If the lock cannot be had within LOCK_WAIT_S, it refuses to run.

Which resource is acted on: every directory inspected (repo, worktree, bare remote) is held open by
descriptor from inspection to removal, and git runs inside that held directory (fchdir + a relative
GIT_DIR), so swapping a pathname after planning cannot redirect a deletion; a pathname that no longer
names the held directory is refused. A worktree is first `git worktree move`d into a fresh random
0700 directory beside it and removed (no --force) only if what arrived there is the inspected one;
otherwise it is moved back. That defeats a substitution at the planned pathname; a process that
enumerates .orchd-worktrees and races the random name is outside this model. If the script dies
mid-step a worktree can be left at .orchd-worktrees/.demo-reset-*/<name>, still registered
(`git worktree list` shows it, `git worktree move` brings it back).
Branch deletes are compare-and-delete on the expected SHA.

Exit codes: 0 clean, 1 something was refused, 2 refused to run at all (open demo task, busy DB, bad input).
Not done: stopping leftover Claude Orchs (`orchd orch-stop`), and unattributed `orchd/*` branches
(listed as skipped, since nothing proves they belong to this demo).
"""
import argparse
import contextlib
import os
import re
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

DEMO_REPOS = ("demo-shop-api", "demo-shop-web", "demo-shop-docs")
TASK_ID = re.compile(r"^[0-9a-f]{8}$")
# no inherited GIT_DIR/GIT_WORK_TREE/GIT_INDEX_FILE/...: they would redirect git away from the held directory
GIT_ENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
GIT_ENV["GIT_OPTIONAL_LOCKS"] = "0"  # `git status` must not touch the index in a dry-run
GIT_TIMEOUT_S = 10
LOCK_WAIT_S = 2  # how long --apply waits for the DB write lock before refusing
LOCK_BUDGET_S = 15  # orchd writers wait 30s for a lock (store.connect); stop starting chains well before
TASK_COLUMNS = "id, repo, repo_path, status, branch, worktree, socket"


class Fatal(Exception):
    pass


def git(repo, *args, check=True):
    out = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env=GIT_ENV,
                         timeout=GIT_TIMEOUT_S)
    if check and out.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {out.stderr.strip()}")
    return out


def is_link(path):
    return Path(path).is_symlink()


def no_symlink_in(path, stop):
    """True when no component of `path` below `stop` (inclusive of path itself) is a symlink."""
    path, stop = Path(path), Path(stop)
    while path != stop:
        if path.is_symlink():
            return False
        if path.parent == path:
            return False
        path = path.parent
    return True


SYSTEM_ALIASES = {"/tmp", "/var", "/etc"}  # macOS: symlinks to /private/<name>; the only ancestors allowed to be links


def root_is_clean(root):
    """Absolute `root` may pass through a symlink only at a system alias (/tmp -> /private/tmp)."""
    p = Path(os.path.abspath(root))
    cur = Path(p.anchor)
    for part in p.relative_to(p.anchor).parts:
        cur = cur / part
        if cur.is_symlink() and str(cur) not in SYSTEM_ALIASES:
            return False
    return True


def node_id(st):
    return (st.st_dev, st.st_ino)


class Held:
    """A directory held open by descriptor from inspection to mutation.

    git runs with its cwd set to the held directory (fchdir) and a relative GIT_DIR, so it acts on
    this directory even if the pathname is swapped later. `same()` says whether the pathname still
    names it; a mutation refuses when it does not."""

    def __init__(self, path):
        self.path = Path(path)
        self.fd = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        self.id = node_id(os.fstat(self.fd))

    def same(self):
        try:
            st = os.lstat(self.path)
        except FileNotFoundError:
            return False
        return stat.S_ISDIR(st.st_mode) and node_id(st) == self.id

    def require(self, what):
        if not self.same():
            raise RuntimeError(f"{what} {self.path} is no longer the directory inspected at planning")

    def git(self, gitdir, *args, check=True):
        """gitdir: '.' for a bare repo, '.git' for a main checkout, None to discover (a linked worktree)."""
        env = dict(GIT_ENV)
        if gitdir:
            env["GIT_DIR"] = gitdir
        out = subprocess.run(["git", *args], capture_output=True, text=True, env=env, timeout=GIT_TIMEOUT_S,
                             pass_fds=(self.fd,), preexec_fn=lambda: os.fchdir(self.fd))
        if check and out.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)}: {out.stderr.strip()}")
        return out

    def ref(self, gitdir, ref):
        r = self.git(gitdir, "rev-parse", "--verify", "--quiet", ref, check=False)
        return r.stdout.strip() if r.returncode == 0 else None

    def close(self):
        os.close(self.fd)


def query_tasks(con, repos):
    marks = ",".join("?" * len(repos))
    return [dict(r) for r in con.execute(f"SELECT {TASK_COLUMNS} FROM tasks WHERE repo IN ({marks})",
                                         tuple(repos)).fetchall()]


def _sig(*paths):
    out = []
    for p in paths:
        try:
            st = os.stat(p)
            out.append((st.st_size, st.st_mtime_ns))
        except FileNotFoundError:
            out.append(None)
    return out


def read_tasks(db, repos):
    """Snapshot read from a private copy of db (+wal): never opens the live file, so no sidecar is created
    or touched next to it. Advisory only: a writer can commit right after (or into a WAL that appears
    after) the copy. --apply re-reads under the write lock before acting."""
    db = Path(db)
    wal = Path(f"{db}-wal")
    before = _sig(db, wal)
    with tempfile.TemporaryDirectory(prefix="demo-reset-db-") as tmp:
        shutil.copy2(db, Path(tmp) / "orchd.db")
        if wal.exists():
            shutil.copy2(wal, Path(tmp) / "orchd.db-wal")
        if _sig(db, wal) != before:  # best effort only; the lock in --apply is the real barrier
            raise Fatal(f"{db} changed while copying (a writer is active): refusing, retry when idle")
        con = sqlite3.connect(Path(tmp) / "orchd.db", timeout=30)  # private copy: sidecars land in tmp
        con.row_factory = sqlite3.Row
        try:
            return query_tasks(con, repos)
        finally:
            con.close()


def load_tasks(home, repos):
    db = Path(home) / "orchd.db"
    if not db.is_file():
        raise Fatal(f"no orchd DB at {db}: cannot prove which resources belong to the demo")
    journal = Path(f"{db}-journal")
    try:
        if journal.exists():
            raise Fatal(f"{journal} exists (interrupted write): refusing to read an uncertain DB")
        return read_tasks(db, repos)
    except sqlite3.Error as e:
        raise Fatal(f"cannot read {db}: {e}")
    except (OSError, shutil.Error) as e:
        raise Fatal(f"cannot copy {db} for a consistent read: {e}")


@contextlib.contextmanager
def write_locked(home):
    """Hold the live DB's write lock without writing: BEGIN IMMEDIATE ... ROLLBACK.

    While held, no writer can create, reopen or change a task; reads on this connection see the current
    committed ledger, WAL included. SQLite creates -wal/-shm while it is open and removes them on close
    when this is the last connection."""
    db = Path(home) / "orchd.db"
    try:
        con = sqlite3.connect(f"file:{db}?mode=rw", uri=True, timeout=LOCK_WAIT_S, isolation_level=None)
    except sqlite3.Error as e:
        raise Fatal(f"cannot open {db}: {e}")
    con.row_factory = sqlite3.Row
    try:
        try:
            con.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as e:
            raise Fatal(f"cannot take the write lock on {db} ({e}): a writer is active, retry when idle")
        try:
            yield con
        finally:
            con.execute("ROLLBACK")
    finally:
        con.close()


def socket_state(path):
    """True live, False provably dead (nothing there / connection refused), None uncertain."""
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(1)
    try:
        s.connect(path)
        return True
    except (FileNotFoundError, ConnectionRefusedError):
        return False
    except OSError:
        return None
    finally:
        s.close()


def registered(repo):
    """[(realpath, branch ref or None)] for every registered worktree of the held repo."""
    out, cur = [], None
    for line in repo.git(".git", "worktree", "list", "--porcelain").stdout.splitlines():
        if line.startswith("worktree "):
            cur = [os.path.realpath(line[9:]), None]
            out.append(cur)
        elif line.startswith("branch ") and cur:
            cur[1] = line[7:]
    return [tuple(c) for c in out]


class Reset:
    def __init__(self, projects, remotes, sock_root):
        self.projects, self.remotes, self.sock_root = Path(projects), Path(remotes), Path(sock_root)
        self.chains = []  # each chain: ordered [(description, callable)]; a failed step stops the rest of ITS chain
        self.refused, self.skipped = [], []
        self.held = []
        self.repos = {}  # repo name -> Held or None (one descriptor per repo, shared by its tasks)
        self.remote_dirs = {}

    @property
    def actions(self):
        return [a for chain in self.chains for a in chain]

    def refuse(self, what, why):
        self.refused.append((what, why))

    def hold(self, path):
        h = Held(path)
        self.held.append(h)
        return h

    def close(self):
        for h in self.held:
            h.close()

    def repo(self, name):
        if name not in self.repos:
            path = self.projects / name
            h = None
            if root_is_clean(self.projects) and not is_link(path) and path.is_dir():
                h = self.hold(path)
                try:
                    if not stat.S_ISDIR(os.stat(".git", dir_fd=h.fd, follow_symlinks=False).st_mode):
                        h = None
                except FileNotFoundError:
                    h = None
            self.repos[name] = h
        return self.repos[name]

    def plan_task(self, task):
        label = f"{task['repo']}/{task['id']}"
        try:
            self._plan_task(task, label)
        except Exception as e:  # an inspection that cannot finish proves nothing: keep everything
            self.refuse(label, f"inspection failed: {e}")

    def _plan_task(self, task, label):
        tid, name = task["id"], task["repo"]
        if not TASK_ID.match(tid):
            return self.refuse(label, "task id is not 8 hex chars")
        if not root_is_clean(self.projects):
            return self.refuse(label, f"projects root {self.projects} passes through a symlink")
        repo_path = self.projects / name
        repo = self.repo(name)
        if repo is None:
            return self.refuse(label, f"{repo_path} is not a real git repo (symlink or missing)")
        branch = f"orchd/{tid}"
        wt = self.projects / ".orchd-worktrees" / f"{name}-{tid}"
        sock_dir = self.sock_root / f"orchd-{tid}"
        # positive ownership: every recorded field must exist and match what we rebuilt (NULL proves nothing)
        for field, expect in (("repo_path", str(repo_path)), ("branch", branch), ("worktree", str(wt)),
                              ("socket", str(sock_dir / "w.sock"))):
            if task[field] != expect:
                return self.refuse(label, f"DB {field} {task[field]!r} != expected {expect!r}")

        problems = []
        gb = []  # worktree + branch chain
        sock_chain = []
        wt_held = None

        # worktrees: every registered one, with the branch it has checked out
        wts = registered(repo)
        ours = [w for w in wts if w[0] == os.path.realpath(wt)]
        for w in wts:
            if w[1] == f"refs/heads/{branch}" and w[0] != os.path.realpath(wt):
                problems.append(f"{branch} is checked out in another worktree {w[0]}")
        head = repo.ref(".git", f"refs/heads/{branch}")
        if os.path.lexists(wt):
            if not no_symlink_in(wt, self.projects):
                problems.append(f"worktree path {wt} involves a symlink")
            elif not ours:
                problems.append(f"{wt} exists but is not a worktree of {name}")
            else:
                wt_held = self.hold(wt)
                dirty = wt_held.git(None, "status", "--porcelain", "--untracked-files=all").stdout.strip()
                if dirty:
                    problems.append("worktree has uncommitted or untracked files")
                if os.path.lexists(wt / ".gitmodules") or wt_held.git(None, "submodule", "status", check=False).stdout.strip():
                    problems.append("worktree has submodules (their data is not checked)")
                if wt_held.git(None, "rev-parse", "--abbrev-ref", "HEAD", check=False).stdout.strip() != branch:
                    problems.append(f"worktree is not on {branch}")
                if ours[0][1] != f"refs/heads/{branch}":
                    problems.append(f"{wt} is registered on {ours[0][1]}, not {branch}")
        elif ours:
            problems.append(f"{wt} is registered but missing; not pruning (a prune is repo-wide), "
                            f"run `git -C {repo_path} worktree prune` yourself if intended")

        # branch: local and remote, each only when provably pushed/merged
        remote = self.remote_dir(repo, name, problems, bool(head))
        remote_sha = main_sha = None
        if remote:
            remote_sha, main_sha = remote.ref(".", f"refs/heads/{branch}"), remote.ref(".", "refs/heads/main")
            if remote.git(".", "symbolic-ref", "HEAD", check=False).stdout.strip() == f"refs/heads/{branch}":
                problems.append(f"remote HEAD points at {branch}")
        if head:
            if not self.pushed(repo, head, remote_sha, main_sha):
                problems.append(f"{branch} has commits that are not on the remote")
        if remote_sha and remote:
            if head and remote_sha != head:
                problems.append(f"remote {branch} ({remote_sha[:8]}) differs from local ({head[:8]})")
            elif not head and not (main_sha and repo.git(".git", "cat-file", "-e", remote_sha, check=False).returncode == 0
                                   and self.merged(repo, remote_sha, main_sha)):
                problems.append(f"remote {branch} has no local copy and is not merged into main")

        if not problems:
            held = [(h, what) for h, what in ((repo, "repo"), (wt_held, "worktree"), (remote, "remote")) if h]
            gb.append((f"verify {name} repo/worktree/remote are the inspected ones",
                       lambda: [h.require(what) for h, what in held]))
            if wt_held:
                gb.append((f"git worktree remove {wt}",
                           lambda: self.remove_worktree(repo, wt_held, branch, head)))
            if head:
                gb.append((f"delete local branch {branch} @{head[:8]}",
                           lambda: self.delete_local_branch(repo, remote, branch, head)))
            if remote_sha:
                gb.append((f"delete remote branch {branch} @{remote_sha[:8]}",
                           lambda: self.delete_remote_branch(remote, branch, remote_sha)))

        # socket dir
        sock_problem = False
        if os.path.lexists(sock_dir):
            if not root_is_clean(self.sock_root):
                problems.append(f"socket root {self.sock_root} passes through a symlink")
                sock_problem = True
            elif is_link(sock_dir) or not sock_dir.is_dir():
                problems.append(f"{sock_dir} is a symlink or not a directory")
                sock_problem = True
            else:
                names = sorted(p.name for p in sock_dir.iterdir())
                sock = sock_dir / "w.sock"
                if any(n != "w.sock" for n in names):
                    problems.append(f"{sock_dir} holds files other than w.sock")
                    sock_problem = True
                elif names and not stat.S_ISSOCK(sock.lstat().st_mode):
                    problems.append(f"{sock} is not a socket")
                    sock_problem = True
                else:
                    state = socket_state(str(sock)) if names else False
                    if state is not False:
                        problems.append(f"{sock} {'still accepts connections (worker alive)' if state else 'could not be proven dead'}")
                        sock_problem = True
                    else:
                        root = Path(os.path.realpath(self.sock_root))
                        ids = (node_id(os.stat(root)), node_id(os.lstat(sock_dir)),
                               node_id(os.lstat(sock)) if names else None)
                        sock_chain.append((f"remove socket dir {sock_dir}",
                                           lambda: self.rm_sock_dir(root, sock_dir.name, ids)))

        for p in problems:
            self.refuse(label, p)
        # any problem keeps the task's whole worktree/branch chain; only a socket problem keeps the socket too
        if len(gb) > 1 and not problems:
            self.chains.append(gb)
        if sock_chain and not sock_problem:
            self.chains.append(sock_chain)

    @staticmethod
    def merged(repo, sha, main_sha):
        return bool(main_sha) and repo.git(".git", "merge-base", "--is-ancestor", sha, main_sha,
                                           check=False).returncode == 0

    def pushed(self, repo, head, remote_sha, main_sha):
        return head == remote_sha or self.merged(repo, head, main_sha)  # on the remote branch or in its main

    def remote_dir(self, repo, name, problems, needed):
        """The held demo bare remote iff the fetch URL and every push URL are exactly it; else None."""
        want = self.remotes / f"{name}.git"
        urls = [repo.git(".git", "remote", "get-url", "origin", check=False).stdout.strip()]
        urls += repo.git(".git", "remote", "get-url", "--push", "--all", "origin", check=False).stdout.split()
        why = None
        if not root_is_clean(self.remotes):
            why = f"remotes root {self.remotes} passes through a symlink"
        else:
            for u in urls:
                if not (u and os.path.isabs(u) and os.path.normpath(u) == str(want) and not is_link(u)):
                    why = f"origin {u!r} is not {want}"
                    break
        if not why and name not in self.remote_dirs:
            held = self.hold(want) if want.is_dir() else None
            if held and held.git(".", "rev-parse", "--is-bare-repository", check=False).stdout.strip() != "true":
                held = None
            self.remote_dirs[name] = held
        if not why and not self.remote_dirs[name]:
            why = f"{want} is not a bare repository"
        if why:
            if needed:
                problems.append(why)
            return None
        return self.remote_dirs[name]

    def remove_worktree(self, repo, wt, branch, head):
        """Move the worktree into a fresh private dir, check it is the inspected one, then remove it there."""
        repo.require("repo")
        wt.require("worktree")
        if repo.ref(".git", f"refs/heads/{branch}") != head:
            raise RuntimeError(f"{branch} moved since planning")
        if (os.path.realpath(wt.path), f"refs/heads/{branch}") not in registered(repo):
            raise RuntimeError(f"{wt.path} is no longer the registered worktree of {branch}")
        quarantine = Path(tempfile.mkdtemp(prefix=".demo-reset-", dir=wt.path.parent))
        dest = quarantine / wt.path.name
        try:
            repo.git(".git", "worktree", "move", str(wt.path), str(dest))
        except Exception:
            with contextlib.suppress(OSError):  # keep the move error, not a cleanup error
                quarantine.rmdir()
            raise
        # nobody else knows `quarantine`: what arrived there stays what we check until we remove it
        arrived = os.lstat(dest)
        if not stat.S_ISDIR(arrived.st_mode) or node_id(arrived) != wt.id \
                or (os.path.realpath(dest), f"refs/heads/{branch}") not in registered(repo):
            raise RuntimeError(self.move_back(repo, dest, wt.path, quarantine,
                                              "a different worktree was at the path when it was moved"))
        try:
            repo.git(".git", "worktree", "remove", str(dest))  # no --force: git refuses dirty/untracked/locked
        except Exception as e:
            raise RuntimeError(self.move_back(repo, dest, wt.path, quarantine, str(e)))
        quarantine.rmdir()

    @staticmethod
    def move_back(repo, dest, origin, quarantine, why):
        try:
            repo.git(".git", "worktree", "move", str(dest), str(origin))
            quarantine.rmdir()
            return f"{why}; moved back to {origin}"
        except Exception as e:
            return f"{why}; could NOT move it back ({e}), it is at {dest}"

    def delete_local_branch(self, repo, remote, branch, head):
        repo.require("repo")
        if any(w[1] == f"refs/heads/{branch}" for w in registered(repo)):
            raise RuntimeError(f"{branch} is checked out in a worktree")
        # the reason it may go (its commits are on the remote) must still hold, on the inspected remote
        if remote is None:
            raise RuntimeError("no proven remote holds its commits")
        remote.require("remote")
        if not self.pushed(repo, head, remote.ref(".", f"refs/heads/{branch}"), remote.ref(".", "refs/heads/main")):
            raise RuntimeError(f"the remote no longer holds {head[:8]}")
        repo.git(".git", "update-ref", "-d", f"refs/heads/{branch}", head)  # compare-and-delete

    @staticmethod
    def delete_remote_branch(remote, branch, sha):
        remote.require("remote")
        # compare-and-delete inside the held bare remote: fails if the ref is no longer exactly `sha`
        remote.git(".", "update-ref", "-d", f"refs/heads/{branch}", sha)

    @staticmethod
    def rm_sock_dir(root, name, ids):
        """Unlink through directory fds bound to the inspected nodes; any substitution aborts."""
        root_id, dir_id, sock_id = ids
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            if node_id(os.fstat(root_fd)) != root_id:
                raise RuntimeError("socket root was replaced since planning")
            dir_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
            try:
                if node_id(os.fstat(dir_fd)) != dir_id:
                    raise RuntimeError("socket dir was replaced since planning")
                names = os.listdir(dir_fd)
                if sock_id is None:
                    if names:
                        raise RuntimeError("socket dir gained files since planning")
                else:
                    if names != ["w.sock"]:
                        raise RuntimeError("socket dir contents changed since planning")
                    st = os.stat("w.sock", dir_fd=dir_fd, follow_symlinks=False)
                    if not stat.S_ISSOCK(st.st_mode) or node_id(st) != sock_id:
                        raise RuntimeError("w.sock was replaced since planning")
                    if socket_state(os.path.join(root, name, "w.sock")) is not False:
                        raise RuntimeError("w.sock is not provably dead")
                    os.unlink("w.sock", dir_fd=dir_fd)
            finally:
                os.close(dir_fd)
            os.rmdir(name, dir_fd=root_fd)
        finally:
            os.close(root_fd)


def leftover_report(projects, repos):
    lines = []
    for repo in repos:
        path = Path(projects) / repo
        if not (path / ".git").exists():
            continue
        branches = git(path, "for-each-ref", "--format=%(refname:short)", "refs/heads").stdout.split()
        wts = [l for l in git(path, "worktree", "list", "--porcelain").stdout.splitlines() if l.startswith("worktree ")]
        lines.append(f"{repo}: {len(branches)} branch(es) {branches}, {len(wts)} worktree(s)")
    return lines


def refuse_open(tasks):
    open_ = [t for t in tasks if t["status"] != "closed"]
    if open_:
        raise Fatal("demo task(s) still open: " + ", ".join(f"{t['id']} ({t['status']})" for t in open_)
                    + " -- close them first")


def build_plan(args, repos, tasks):
    plan = Reset(args.projects, args.remotes, args.sock_root)
    for t in tasks:
        plan.plan_task(t)
    for repo in repos:
        repo_path = Path(args.projects) / repo
        if (repo_path / ".git").is_dir() and not is_link(repo_path):
            known = {f"orchd/{t['id']}" for t in tasks if t["repo"] == repo}
            for b in git(repo_path, "for-each-ref", "--format=%(refname:short)", "refs/heads/orchd").stdout.split():
                if b not in known:
                    plan.skipped.append(f"{repo}: {b} (not attributed to any demo task in the DB)")
    return plan


def execute(plan, apply, deadline=None):
    print(("APPLY" if apply else "DRY-RUN (nothing changed; pass --apply to remove)") + f" -- {len(plan.actions)} action(s)")
    for chain in plan.chains:
        if apply and time.monotonic() > deadline:
            for desc, _ in chain:
                plan.refuse(desc, f"not attempted: the DB write lock was held for {LOCK_BUDGET_S}s; rerun to continue")
            continue
        for i, (desc, fn) in enumerate(chain):
            if not apply:
                print(f"  would {desc}")
                continue
            try:
                fn()
                print(f"  done: {desc}")
            except Exception as e:
                # a failed step stops the rest of this task's chain: never delete a branch behind a failed remove
                plan.refuse(desc, f"failed: {e}")
                for later, _ in chain[i + 1:]:
                    plan.refuse(later, "not attempted: an earlier step for this task failed")
                break


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="really remove (default: dry-run)")
    ap.add_argument("--home", default=os.environ.get("ORCHD_HOME", str(Path.home() / ".local/share/orchd")))
    ap.add_argument("--projects", default=os.environ.get("ORCHD_PROJECTS", str(Path.home() / "projects")))
    ap.add_argument("--remotes", default=str(Path.home() / ".local/share/orchd-demo/remotes"))
    ap.add_argument("--sock-root", default="/tmp")
    ap.add_argument("--repo", action="append", help=f"demo repo name, must start with demo-shop- (default {DEMO_REPOS})")
    args = ap.parse_args(argv)
    repos = tuple(args.repo or DEMO_REPOS)
    plan = None
    try:
        for r in repos:
            if not re.fullmatch(r"demo-shop-[a-z0-9-]+", r):
                raise Fatal(f"{r!r} is not a demo-shop-* repo")
        tasks = load_tasks(args.home, repos)
        refuse_open(tasks)  # an open task in the snapshot stops here, before the live DB is opened
        if not args.apply:
            plan = build_plan(args, repos, tasks)
            execute(plan, apply=False)
        else:
            with write_locked(args.home) as con:
                deadline = time.monotonic() + LOCK_BUDGET_S
                try:
                    tasks = query_tasks(con, repos)  # the authoritative ledger: no writer can change it now
                except sqlite3.Error as e:
                    raise Fatal(f"cannot read the locked DB: {e}")
                refuse_open(tasks)
                plan = build_plan(args, repos, tasks)
                execute(plan, apply=True, deadline=deadline)
    except Fatal as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    finally:
        if plan:
            plan.close()

    for what, why in plan.refused:
        print(f"  REFUSED {what}: {why}")
    for s in plan.skipped:
        print(f"  SKIPPED {s}")
    if args.apply:
        print("left over:")
        for line in leftover_report(args.projects, repos):
            print("  " + line)
    return 1 if plan.refused else 0


if __name__ == "__main__":
    sys.exit(main())
