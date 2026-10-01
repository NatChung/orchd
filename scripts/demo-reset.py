#!/usr/bin/env python3
"""Reset the tech-sharing demo (demo-shop-api / -web / -docs) after a run.

Dry-run by default: prints what it would remove and changes nothing. `--apply` removes it.

Only resources that the orchd DB attributes to a *closed* demo-shop task are eligible: the task's
worktree, its local `orchd/<id>` branch, the same branch on the demo remote, and its /tmp socket dir.
Paths are rebuilt from the task id, never taken from the DB or a glob. Anything that could lose data
(dirty/untracked/unpushed work, submodules, a live socket, a symlink, a path or remote that is not
exactly where the demo puts it) is refused with the reason; the rest still proceeds.
The DB is opened read-only. Nothing here forces git: no --force, no branch -D.

Exit codes: 0 clean, 1 something was refused, 2 refused to run at all (open demo task, bad input).
Not done: stopping leftover Claude Orchs (`orchd orch-stop`), and unattributed `orchd/*` branches
(listed as skipped, since nothing proves they belong to this demo).
"""
import argparse
import os
import re
import socket
import sqlite3
import stat
import subprocess
import sys
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


def load_tasks(home, repos):
    db = Path(home) / "orchd.db"
    if not db.is_file():
        raise Fatal(f"no orchd DB at {db}: cannot prove which resources belong to the demo")
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
        con.row_factory = sqlite3.Row
        marks = ",".join("?" * len(repos))
        try:
            return con.execute(f"SELECT id, repo, status, branch, worktree, socket FROM tasks WHERE repo IN ({marks})",
                               tuple(repos)).fetchall()
        finally:
            con.close()
    except sqlite3.Error as e:
        raise Fatal(f"cannot read {db} read-only: {e}")


def socket_live(path):
    if not os.path.exists(path):
        return False
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(1)
    try:
        s.connect(path)
        return True
    except OSError:
        return False
    finally:
        s.close()


class Reset:
    def __init__(self, projects, remotes, sock_root):
        self.projects, self.remotes, self.sock_root = Path(projects), Path(remotes), Path(sock_root)
        self.actions, self.refused, self.skipped = [], [], []  # actions: (description, callable)

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
        # the DB must agree with the paths we rebuilt, or it is not provably this task's resource
        for field, expect in (("branch", branch), ("worktree", str(wt)), ("socket", str(sock_dir / "w.sock"))):
            if task[field] not in (None, expect):
                return self.refuse(label, f"DB {field} {task[field]!r} != expected {expect!r}")

        problems = []
        steps = []
        head = None

        # worktree
        registered = {line[9:] for line in git(repo_path, "worktree", "list", "--porcelain").stdout.splitlines()
                      if line.startswith("worktree ")}
        if os.path.lexists(wt):
            if not no_symlink_in(wt, self.projects):
                problems.append(f"worktree path {wt} involves a symlink")
            elif str(wt) not in registered:
                problems.append(f"{wt} exists but is not a worktree of {repo}")
            else:
                dirty = git(wt, "status", "--porcelain", "--untracked-files=all").stdout.strip()
                if dirty:
                    problems.append("worktree has uncommitted or untracked files")
                if (wt / ".gitmodules").exists() or git(wt, "submodule", "status", check=False).stdout.strip():
                    problems.append("worktree has submodules (their data is not checked)")
                if git(wt, "rev-parse", "--abbrev-ref", "HEAD", check=False).stdout.strip() != branch:
                    problems.append(f"worktree is not on {branch}")
                steps.append((f"git worktree remove {wt}", lambda: git(repo_path, "worktree", "remove", str(wt))))
        elif str(wt) in registered:
            steps.append((f"git worktree prune (registered but missing: {wt})",
                          lambda: git(repo_path, "worktree", "prune")))

        # branch: local and remote, each only when provably pushed/merged
        local = git(repo_path, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False)
        head = local.stdout.strip() if local.returncode == 0 else None
        origin = git(repo_path, "remote", "get-url", "origin", check=False).stdout.strip()
        remote_ok = origin and Path(origin).resolve().parent == self.remotes.resolve() \
            and Path(origin).name == f"{repo}.git" and not is_link(origin)
        remote_sha = main_sha = None
        if not remote_ok:
            if head:
                problems.append(f"origin {origin!r} is not {self.remotes}/{repo}.git")
        else:
            ls = git(repo_path, "ls-remote", "origin", f"refs/heads/{branch}", "refs/heads/main").stdout.split()
            refs = dict(zip(ls[1::2], ls[0::2]))
            remote_sha, main_sha = refs.get(f"refs/heads/{branch}"), refs.get("refs/heads/main")
        if head:
            pushed = head == remote_sha
            if not pushed and main_sha and git(repo_path, "merge-base", "--is-ancestor", head, main_sha,
                                               check=False).returncode == 0:
                pushed = True  # already contained in the remote main
            if not pushed:
                problems.append(f"{branch} has commits that are not on the remote")
            else:
                steps.append((f"delete local branch {branch} @{head[:8]}",
                              lambda: git(repo_path, "update-ref", "-d", f"refs/heads/{branch}", head)))
        if remote_sha:
            if head and remote_sha != head:
                problems.append(f"remote {branch} ({remote_sha[:8]}) differs from local ({head[:8]})")
            elif not head and not (main_sha and git(repo_path, "cat-file", "-e", remote_sha, check=False).returncode == 0
                                   and git(repo_path, "merge-base", "--is-ancestor", remote_sha, main_sha,
                                           check=False).returncode == 0):
                problems.append(f"remote {branch} has no local copy and is not merged into main")
            else:
                steps.append((f"delete remote branch {branch} @{remote_sha[:8]}",
                              lambda: git(repo_path, "push", "origin", "--delete", branch)))

        # socket dir
        if os.path.lexists(sock_dir):
            names = []
            if is_link(sock_dir) or not sock_dir.is_dir():
                problems.append(f"{sock_dir} is a symlink or not a directory")
            else:
                names = sorted(p.name for p in sock_dir.iterdir())
                sock = sock_dir / "w.sock"
                if any(n != "w.sock" for n in names):
                    problems.append(f"{sock_dir} holds files other than w.sock")
                elif names and not stat.S_ISSOCK(sock.lstat().st_mode):
                    problems.append(f"{sock} is not a socket")
                elif socket_live(str(sock)):
                    problems.append(f"{sock} still accepts connections (worker alive)")
                else:
                    steps.append((f"remove socket dir {sock_dir}", lambda: self.rm_sock_dir(sock_dir, names)))

        if problems:
            # keep going only for steps that are independent of the failure: refuse the whole task's
            # worktree/branch chain so we never leave a branch behind a half-removed worktree
            for p in problems:
                self.refuse(label, p)
            steps = [s for s in steps if s[0].startswith("remove socket dir")] if not any(
                "socket" in p or "w.sock" in p for p in problems) else []
        self.actions.extend(steps)

    @staticmethod
    def rm_sock_dir(sock_dir, names):
        for n in names:
            os.unlink(sock_dir / n)
        os.rmdir(sock_dir)


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
    for desc, fn in plan.actions:
        if not args.apply:
            print(f"  would {desc}")
            continue
        try:
            fn()
            print(f"  done: {desc}")
        except Exception as e:  # keep going: the report says what is left
            plan.refuse(desc, f"failed: {e}")
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
