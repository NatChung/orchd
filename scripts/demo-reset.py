#!/usr/bin/env python3
"""Reset the tech-sharing demo (demo-shop-api / -web / -docs) after a run.

Dry-run by default: prints what it would remove and changes nothing. `--apply` removes it.

Only resources that the orchd DB attributes to a *closed* demo-shop task are eligible: the task's
worktree, its local `orchd/<id>` branch, the same branch on the demo remote, and its /tmp socket dir.
Paths are rebuilt from the task id, never taken from the DB or a glob. Anything that could lose data
(dirty/untracked/unpushed work, submodules, a live socket, a symlink, a path or remote that is not
exactly where the demo puts it) is refused with the reason; the rest still proceeds.
The DB is read without creating sidecar files (immutable read, or a private copy when a WAL exists). Nothing here forces git: no --force, no branch -D.

Exit codes: 0 clean, 1 something was refused, 2 refused to run at all (open demo task, bad input).
Not done: stopping leftover Claude Orchs (`orchd orch-stop`), and unattributed `orchd/*` branches
(listed as skipped, since nothing proves they belong to this demo).
"""
import argparse
import os
import re
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

DEMO_REPOS = ("demo-shop-api", "demo-shop-web", "demo-shop-docs")
TASK_ID = re.compile(r"^[0-9a-f]{8}$")
GIT_ENV = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}  # `git status` must not touch the index in a dry-run


class Fatal(Exception):
    pass


def git(repo, *args, check=True):
    out = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env=GIT_ENV, timeout=60)
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


def read_tasks(db, repos):
    con = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        marks = ",".join("?" * len(repos))
        return con.execute(f"SELECT id, repo, repo_path, status, branch, worktree, socket FROM tasks "
                           f"WHERE repo IN ({marks})", tuple(repos)).fetchall()
    finally:
        con.close()


def _sig(*paths):
    out = []
    for p in paths:
        try:
            st = os.stat(p)
            out.append((st.st_size, st.st_mtime_ns))
        except FileNotFoundError:
            out.append(None)
    return out


def load_tasks(home, repos):
    """Read the DB without creating or touching any file next to it (no -wal/-shm sidecars).

    A cleanly closed DB has no -wal/-shm, so an immutable read is exact. If a WAL exists, its content is
    part of the truth, so the db+wal pair is copied to a private temp dir and read there; if either file
    changes while copying, or a rollback journal exists, refuse rather than read an inconsistent view."""
    db = Path(home) / "orchd.db"
    if not db.is_file():
        raise Fatal(f"no orchd DB at {db}: cannot prove which resources belong to the demo")
    wal, shm, journal = Path(f"{db}-wal"), Path(f"{db}-shm"), Path(f"{db}-journal")
    try:
        if journal.exists():
            raise Fatal(f"{journal} exists (interrupted write): refusing to read an uncertain DB")
        if not wal.exists() and not shm.exists():
            return read_tasks(db, repos)
        before = _sig(db, wal)
        with tempfile.TemporaryDirectory(prefix="demo-reset-db-") as tmp:
            shutil.copy2(db, Path(tmp) / "orchd.db")
            if wal.exists():
                shutil.copy2(wal, Path(tmp) / "orchd.db-wal")
            if _sig(db, wal) != before:
                raise Fatal(f"{db} changed while reading (a writer is active): refusing, retry when idle")
            con = sqlite3.connect(Path(tmp) / "orchd.db", timeout=30)  # private copy: sidecars land in tmp
            con.row_factory = sqlite3.Row
            try:
                marks = ",".join("?" * len(repos))
                return [dict(r) for r in con.execute(
                    f"SELECT id, repo, repo_path, status, branch, worktree, socket FROM tasks WHERE repo IN ({marks})",
                    tuple(repos)).fetchall()]
            finally:
                con.close()
    except sqlite3.Error as e:
        raise Fatal(f"cannot read {db}: {e}")
    except (OSError, shutil.Error) as e:
        raise Fatal(f"cannot copy {db} for a consistent read: {e}")


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


def node_id(st):
    return (st.st_dev, st.st_ino)


class Reset:
    def __init__(self, projects, remotes, sock_root):
        self.projects, self.remotes, self.sock_root = Path(projects), Path(remotes), Path(sock_root)
        self.chains = []  # each chain: ordered [(description, callable)]; a failed step stops the rest of ITS chain
        self.refused, self.skipped = [], []

    @property
    def actions(self):
        return [a for chain in self.chains for a in chain]

    def refuse(self, what, why):
        self.refused.append((what, why))

    def plan_task(self, task):
        tid, repo = task["id"], task["repo"]
        label = f"{repo}/{tid}"
        if not TASK_ID.match(tid):
            return self.refuse(label, "task id is not 8 hex chars")
        repo_path = self.projects / repo
        if is_link(repo_path) or not (repo_path / ".git").is_dir():
            return self.refuse(label, f"{repo_path} is not a real git repo (symlink or missing)")
        branch = f"orchd/{tid}"
        wt = self.projects / ".orchd-worktrees" / f"{repo}-{tid}"
        sock_dir = self.sock_root / f"orchd-{tid}"
        # positive ownership: every recorded field must exist and match what we rebuilt (NULL proves nothing)
        for field, expect in (("repo_path", str(repo_path)), ("branch", branch), ("worktree", str(wt)),
                              ("socket", str(sock_dir / "w.sock"))):
            if task[field] != expect:
                return self.refuse(label, f"DB {field} {task[field]!r} != expected {expect!r}")

        problems = []
        gb = []  # worktree + branch chain
        sock_chain = []

        # worktrees: every registered one, with the branch it has checked out
        wts = self.registered(repo_path)
        ours = [w for w in wts if w[0] == os.path.realpath(wt)]
        using = [w for w in wts if w[1] == f"refs/heads/{branch}"]
        foreign_use = [w for w in using if w[0] != os.path.realpath(wt)]
        for w in foreign_use:
            problems.append(f"{branch} is checked out in another worktree {w[0]}")
        local = git(repo_path, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False)
        head = local.stdout.strip() if local.returncode == 0 else None
        if os.path.lexists(wt):
            if not no_symlink_in(wt, self.projects):
                problems.append(f"worktree path {wt} involves a symlink")
            elif not ours:
                problems.append(f"{wt} exists but is not a worktree of {repo}")
            else:
                dirty = git(wt, "status", "--porcelain", "--untracked-files=all").stdout.strip()
                if dirty:
                    problems.append("worktree has uncommitted or untracked files")
                if (wt / ".gitmodules").exists() or git(wt, "submodule", "status", check=False).stdout.strip():
                    problems.append("worktree has submodules (their data is not checked)")
                if git(wt, "rev-parse", "--abbrev-ref", "HEAD", check=False).stdout.strip() != branch:
                    problems.append(f"worktree is not on {branch}")
                gb.append((f"git worktree remove {wt}", lambda: self.remove_worktree(repo_path, wt, branch, head)))
        elif ours:
            problems.append(f"{wt} is registered but missing; not pruning (a prune is repo-wide), "
                            f"run `git -C {repo_path} worktree prune` yourself if intended")

        # branch: local and remote, each only when provably pushed/merged
        remote_dir = self.remote_dir(repo_path, repo, problems, bool(head))
        remote_sha = main_sha = None
        if remote_dir:
            refs = {}
            for ref in (f"refs/heads/{branch}", "refs/heads/main"):
                r = git(remote_dir, "rev-parse", "--verify", "--quiet", ref, check=False)
                refs[ref] = r.stdout.strip() if r.returncode == 0 else None
            remote_sha, main_sha = refs[f"refs/heads/{branch}"], refs["refs/heads/main"]
            if git(remote_dir, "symbolic-ref", "HEAD", check=False).stdout.strip() == f"refs/heads/{branch}":
                problems.append(f"remote HEAD points at {branch}")
        if head:
            pushed = head == remote_sha
            if not pushed and main_sha and git(repo_path, "merge-base", "--is-ancestor", head, main_sha,
                                               check=False).returncode == 0:
                pushed = True  # already contained in the remote main
            if not pushed:
                problems.append(f"{branch} has commits that are not on the remote")
            else:
                gb.append((f"delete local branch {branch} @{head[:8]}",
                           lambda: self.delete_local_branch(repo_path, branch, head)))
        if remote_sha and remote_dir:
            if head and remote_sha != head:
                problems.append(f"remote {branch} ({remote_sha[:8]}) differs from local ({head[:8]})")
            elif not head and not (main_sha and git(repo_path, "cat-file", "-e", remote_sha, check=False).returncode == 0
                                   and git(repo_path, "merge-base", "--is-ancestor", remote_sha, main_sha,
                                           check=False).returncode == 0):
                problems.append(f"remote {branch} has no local copy and is not merged into main")
            else:
                gb.append((f"delete remote branch {branch} @{remote_sha[:8]}",
                           lambda: self.delete_remote_branch(remote_dir, branch, remote_sha)))

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
        if gb and not problems:
            self.chains.append(gb)
        if sock_chain and not sock_problem:
            self.chains.append(sock_chain)

    @staticmethod
    def registered(repo_path):
        """[(realpath, branch ref or None)] for every registered worktree of the repo."""
        out, cur = [], None
        for line in git(repo_path, "worktree", "list", "--porcelain").stdout.splitlines():
            if line.startswith("worktree "):
                cur = [os.path.realpath(line[9:]), None]
                out.append(cur)
            elif line.startswith("branch ") and cur:
                cur[1] = line[7:]
        return [tuple(c) for c in out]

    def remote_dir(self, repo_path, repo, problems, needed):
        """The demo bare remote iff the fetch URL and every push URL are exactly it; else None."""
        want = self.remotes.resolve() / f"{repo}.git"
        urls = [git(repo_path, "remote", "get-url", "origin", check=False).stdout.strip()]
        urls += git(repo_path, "remote", "get-url", "--push", "--all", "origin", check=False).stdout.split()
        for u in urls:
            ok = u and os.path.isabs(u) and Path(u).name == f"{repo}.git" and not is_link(u) \
                and Path(u).resolve() == want
            if not ok:
                if needed:
                    problems.append(f"origin {u!r} is not {self.remotes}/{repo}.git")
                return None
        if git(want, "rev-parse", "--is-bare-repository", check=False).stdout.strip() != "true":
            if needed:
                problems.append(f"{want} is not a bare repository")
            return None
        return want

    def remove_worktree(self, repo_path, wt, branch, head):
        cur = git(repo_path, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False).stdout.strip()
        if cur != head:
            raise RuntimeError(f"{branch} moved since planning ({cur[:8]} != {head[:8]})")
        git(repo_path, "worktree", "remove", str(wt))  # no --force: git itself refuses dirty/untracked/locked

    def delete_local_branch(self, repo_path, branch, head):
        if any(w[1] == f"refs/heads/{branch}" for w in self.registered(repo_path)):
            raise RuntimeError(f"{branch} is checked out in a worktree")
        git(repo_path, "update-ref", "-d", f"refs/heads/{branch}", head)  # compare-and-delete

    @staticmethod
    def delete_remote_branch(remote_dir, branch, sha):
        # compare-and-delete on the proven bare remote: fails if the ref is no longer exactly `sha`
        git(remote_dir, "update-ref", "-d", f"refs/heads/{branch}", sha)

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
    try:
        for r in repos:
            if not re.fullmatch(r"demo-shop-[a-z0-9-]+", r):
                raise Fatal(f"{r!r} is not a demo-shop-* repo")
        tasks = load_tasks(args.home, repos)
        open_ = [t for t in tasks if t["status"] != "closed"]
        if open_:
            raise Fatal("demo task(s) still open: " + ", ".join(f"{t['id']} ({t['status']})" for t in open_)
                        + " -- close them first")
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
    except Fatal as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2

    print(("APPLY" if args.apply else "DRY-RUN (nothing changed; pass --apply to remove)") + f" -- {len(plan.actions)} action(s)")
    for chain in plan.chains:
        for i, (desc, fn) in enumerate(chain):
            if not args.apply:
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
