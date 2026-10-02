"""scripts/demo-reset.py against a scratch ORCHD_HOME / projects / remotes / socket root. Nothing real is touched."""
import importlib.util
import io
import os
import socket
import subprocess
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from orchd import store

SPEC = importlib.util.spec_from_file_location("demo_reset", Path(__file__).resolve().parents[1] / "scripts/demo-reset.py")
demo_reset = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(demo_reset)

REPO = "demo-shop-api"


def sh(*cmd, cwd=None):
    return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True,
                          env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}).stdout.strip()


class DemoResetTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dr-", dir="/tmp")).resolve()  # short: AF_UNIX paths max ~104 bytes
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.tmp)]))
        t = self.tmp
        self.home, self.projects, self.remotes, self.socks = t / "home", t / "projects", t / "remotes", t / "socks"
        for d in (self.home, self.projects, self.remotes, self.socks):
            d.mkdir()
        self.remote = self.remotes / f"{REPO}.git"
        sh("git", "init", "--bare", "-b", "main", str(self.remote))
        self.repo = self.projects / REPO
        sh("git", "init", "-b", "main", str(self.repo))
        sh("git", "remote", "add", "origin", str(self.remote), cwd=self.repo)
        (self.repo / "a.txt").write_text("seed\n")
        sh("git", "add", ".", cwd=self.repo)
        sh("git", "commit", "-m", "seed", cwd=self.repo)
        sh("git", "push", "-u", "origin", "main", cwd=self.repo)
        self.con = store.connect(self.home / "orchd.db")
        self.addCleanup(self.con.close)
        self.n = 0

    def make_task(self, status="closed", push=True, commit=True):
        self.n += 1
        tid = f"{self.n:08x}"
        branch, wt = f"orchd/{tid}", self.projects / ".orchd-worktrees" / f"{REPO}-{tid}"
        sh("git", "worktree", "add", "-b", branch, str(wt), "main", cwd=self.repo)
        if commit:
            (wt / f"{tid}.txt").write_text("work\n")
            sh("git", "add", ".", cwd=wt)
            sh("git", "commit", "-m", "work", cwd=wt)
        if push:
            sh("git", "push", "origin", branch, cwd=wt)
        sdir = self.socks / f"orchd-{tid}"
        sdir.mkdir()
        s = socket.socket(socket.AF_UNIX)
        s.bind(str(sdir / "w.sock"))
        s.close()  # bound then closed: a stale socket file, nobody listening
        store.create_task(self.con, id=tid, repo=REPO, repo_path=str(self.repo), title="t", instructions="i",
                          done_when="d", orch_thread="o", codex_bin="c", branch=branch, worktree=str(wt),
                          socket=str(sdir / "w.sock"), status=status)
        return tid, wt, sdir

    def run_reset(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = demo_reset.main(["--home", str(self.home), "--projects", str(self.projects), "--remotes",
                                    str(self.remotes), "--sock-root", str(self.socks), *extra])
        return code, out.getvalue() + err.getvalue()

    def snapshot(self):
        files = sorted(str(p.relative_to(self.tmp)) for p in self.tmp.rglob("*") if ".git/" not in str(p) or "refs" in str(p))
        refs = sh("git", "for-each-ref", cwd=self.repo) + sh("git", "ls-remote", str(self.remote))
        return files, refs, sh("git", "worktree", "list", cwd=self.repo)

    def branches(self):
        return sh("git", "for-each-ref", "--format=%(refname:short)", "refs/heads", cwd=self.repo).split()

    def remote_branches(self):
        return [l.split()[1] for l in sh("git", "ls-remote", "--heads", str(self.remote)).splitlines()]

    def test_dry_run_changes_nothing(self):
        self.make_task()
        before = self.snapshot()
        code, out = self.run_reset()
        self.assertEqual(code, 0, out)
        self.assertIn("would git worktree remove", out)
        self.assertIn("would delete remote branch", out)
        self.assertEqual(self.snapshot(), before)

    def test_apply_removes_only_clean_pushed_closed_task(self):
        tid, wt, sdir = self.make_task()
        code, out = self.run_reset("--apply")
        self.assertEqual(code, 0, out)
        self.assertFalse(wt.exists())
        self.assertFalse(sdir.exists())
        self.assertEqual(self.branches(), ["main"])
        self.assertEqual(self.remote_branches(), ["refs/heads/main"])
        self.assertEqual(sh("git", "worktree", "list", "--porcelain", cwd=self.repo).count("worktree "), 1)
        self.assertEqual((self.repo / "a.txt").read_text(), "seed\n")

    def test_open_task_refuses_everything(self):
        _, wt, _ = self.make_task()
        self.make_task(status="running")
        code, out = self.run_reset("--apply")
        self.assertEqual(code, 2)
        self.assertIn("still open", out)
        self.assertTrue(wt.exists())

    def test_dirty_untracked_and_unpushed_are_kept(self):
        _, dirty, _ = self.make_task()
        (dirty / f"{dirty.name[-8:]}.txt").write_text("edit\n")
        _, untracked, _ = self.make_task()
        (untracked / "scratch.txt").write_text("x\n")
        _, unpushed, _ = self.make_task(push=False)
        _, clean, sdir = self.make_task()
        code, out = self.run_reset("--apply")
        self.assertEqual(code, 1, out)
        for kept in (dirty, untracked, unpushed):
            self.assertTrue(kept.exists(), kept)
        self.assertEqual((dirty / f"{dirty.name[-8:]}.txt").read_text(), "edit\n")
        self.assertTrue((untracked / "scratch.txt").exists())
        self.assertFalse(clean.exists())
        self.assertIn("uncommitted or untracked", out)
        self.assertIn("not on the remote", out)
        self.assertEqual(len(self.branches()), 4)  # main + the three refused tasks
        self.assertEqual(len(self.remote_branches()), 3)  # main + dirty + untracked (unpushed never was; clean is gone)

    def test_submodule_worktree_is_refused(self):
        _, wt, _ = self.make_task()
        (wt / ".gitmodules").write_text('[submodule "x"]\n\tpath = x\n\turl = ../x\n')
        sh("git", "add", ".gitmodules", cwd=wt)
        sh("git", "commit", "-m", "gitmodules", cwd=wt)
        sh("git", "push", "origin", "HEAD", cwd=wt)
        code, out = self.run_reset("--apply")
        self.assertEqual(code, 1)
        self.assertIn("submodules", out)
        self.assertTrue(wt.exists())

    def test_symlinked_worktree_and_socket_dir_are_refused(self):
        tid, wt, sdir = self.make_task()
        precious = self.tmp / "precious"
        precious.mkdir()
        (precious / "keep.txt").write_text("keep\n")
        # swap the socket dir for a symlink to a directory that is not ours
        for p in sdir.iterdir():
            p.unlink()
        sdir.rmdir()
        sdir.symlink_to(precious)
        code, out = self.run_reset("--apply")
        self.assertEqual(code, 1)
        self.assertIn("symlink", out)
        self.assertEqual((precious / "keep.txt").read_text(), "keep\n")
        self.assertTrue(sdir.is_symlink())

    def test_foreign_files_in_socket_dir_are_kept(self):
        _, _, sdir = self.make_task()
        (sdir / "notes.txt").write_text("hi\n")
        code, out = self.run_reset("--apply")
        self.assertEqual(code, 1)
        self.assertTrue((sdir / "notes.txt").exists())
        self.assertTrue((sdir / "w.sock").exists())

    def test_live_socket_is_refused(self):
        _, wt, sdir = self.make_task()
        (sdir / "w.sock").unlink()
        srv = socket.socket(socket.AF_UNIX)
        srv.bind(str(sdir / "w.sock"))
        srv.listen(1)
        self.addCleanup(srv.close)
        code, out = self.run_reset("--apply")
        self.assertEqual(code, 1)
        self.assertIn("accepts connections", out)
        self.assertTrue((sdir / "w.sock").exists())

    def test_db_paths_that_disagree_are_refused(self):
        tid, wt, _ = self.make_task()
        self.con.execute("UPDATE tasks SET worktree=? WHERE id=?", (str(self.tmp / "elsewhere"), tid))
        code, out = self.run_reset("--apply")
        self.assertEqual(code, 1)
        self.assertIn("DB worktree", out)
        self.assertTrue(wt.exists())

    def test_branch_without_task_row_is_skipped_not_deleted(self):
        sh("git", "branch", "orchd/deadbeef", cwd=self.repo)
        code, out = self.run_reset("--apply")
        self.assertEqual(code, 0, out)
        self.assertIn("SKIPPED", out)
        self.assertIn("orchd/deadbeef", self.branches())

    def test_remote_outside_remotes_dir_is_refused(self):
        _, wt, _ = self.make_task()
        other = self.tmp / "other.git"
        sh("git", "clone", "--bare", str(self.remote), str(other))
        sh("git", "remote", "set-url", "origin", str(other), cwd=self.repo)
        code, out = self.run_reset("--apply")
        self.assertEqual(code, 1)
        self.assertIn("is not", out)
        self.assertTrue(wt.exists())

    def test_non_demo_repo_name_is_rejected(self):
        code, out = self.run_reset("--repo", "orchd")
        self.assertEqual(code, 2)
        self.assertIn("not a demo-shop", out)

    def test_missing_db_refuses(self):
        (self.home / "orchd.db").unlink()
        for f in self.home.glob("orchd.db*"):
            f.unlink()
        code, out = self.run_reset()
        self.assertEqual(code, 2)
        self.assertIn("no orchd DB", out)

    def test_safety_repo_path_mismatch(self):
        tid, wt, _ = self.make_task()
        self.con.execute('UPDATE tasks SET repo_path=? WHERE id=?',
                         (str(self.tmp / 'other-projects' / REPO), tid))
        code, out = self.run_reset('--apply')
        self.assertTrue(wt.exists(), f'Foreign repo_path accepted; exit={code}\n{out}')

    def test_safety_remote_changed_after_plan(self):
        tid, wt, _ = self.make_task()
        branch = f'orchd/{tid}'
        original = sh('git', 'rev-parse', 'HEAD', cwd=wt)
        plan_task = demo_reset.Reset.plan_task
        def mutate_after_plan(plan, task):
            plan_task(plan, task)
            (wt / 'later.txt').write_text('new remote data\n')
            sh('git', 'add', '.', cwd=wt)
            sh('git', 'commit', '-m', 'concurrent remote update', cwd=wt)
            sh('git', 'push', 'origin', branch, cwd=wt)
            self.new_remote = sh('git', 'rev-parse', 'HEAD', cwd=wt)
            sh('git', 'reset', '--hard', original, cwd=wt)
        with patch.object(demo_reset.Reset, 'plan_task', mutate_after_plan):
            code, out = self.run_reset('--apply')
        actual = sh('git', 'ls-remote', str(self.remote), f'refs/heads/{branch}')
        self.assertIn(self.new_remote, actual, f'Concurrent remote commit deleted; exit={code}\n{out}')

    def test_safety_remove_failure_keeps_branches(self):
        tid, wt, _ = self.make_task()
        branch = f'orchd/{tid}'
        plan_task = demo_reset.Reset.plan_task
        def dirty_after_plan(plan, task):
            plan_task(plan, task)
            (wt / 'untracked-after-plan.txt').write_text('keep\n')
        with patch.object(demo_reset.Reset, 'plan_task', dirty_after_plan):
            code, out = self.run_reset('--apply')
        self.assertTrue(wt.exists(), out)
        self.assertIn(branch, self.branches(), f'Failed remove still deleted local branch; exit={code}\n{out}')

    def test_safety_moved_dirty_worktree_keeps_branch(self):
        tid, wt, _ = self.make_task()
        moved = self.tmp / 'elsewhere'
        sh('git', 'worktree', 'move', str(wt), str(moved), cwd=self.repo)
        (moved / 'untracked.txt').write_text('keep\n')
        code, out = self.run_reset('--apply')
        self.assertIn(f'orchd/{tid}', self.branches(), f'Dirty moved worktree branch deleted; exit={code}\n{out}')

    def test_safety_symlink_socket_parent(self):
        tid, _, _ = self.make_task()
        self.socks.rename(self.tmp / 'original-socks')
        foreign = self.tmp / 'foreign-socks'
        target = foreign / f'orchd-{tid}'
        target.mkdir(parents=True)
        sock = target / 'w.sock'
        server = socket.socket(socket.AF_UNIX)
        server.bind(str(sock))
        server.close()
        self.socks.symlink_to(foreign, target_is_directory=True)
        code, out = self.run_reset('--apply')
        self.assertTrue(sock.exists(), f'Socket through symlinked parent deleted; exit={code}\n{out}')

    def test_safety_dry_run_no_sqlite_sidecars(self):
        self.make_task()
        self.con.close()
        before = sorted(p.name for p in self.home.iterdir())
        code, out = self.run_reset()
        after = sorted(p.name for p in self.home.iterdir())
        self.assertEqual(before, after, f'Dry run created SQLite sidecars; exit={code}\n{out}')

    def test_safety_foreign_pushurl(self):
        tid, _, _ = self.make_task()
        foreign = self.tmp / 'foreign.git'
        sh('git', 'clone', '--bare', str(self.remote), str(foreign))
        sh('git', 'remote', 'set-url', '--push', 'origin', str(foreign), cwd=self.repo)
        code, out = self.run_reset('--apply')
        actual = sh('git', 'ls-remote', str(foreign), f'refs/heads/orchd/{tid}')
        self.assertTrue(actual, f'Foreign push destination deleted; exit={code}\n{out}')

    def test_safety_socket_replaced_after_plan(self):
        _, _, sdir = self.make_task()
        sock = sdir / 'w.sock'
        plan_task = demo_reset.Reset.plan_task
        def replace_after_plan(plan, task):
            plan_task(plan, task)
            sock.unlink()
            sock.write_text('foreign regular file\n')
        with patch.object(demo_reset.Reset, 'plan_task', replace_after_plan):
            code, out = self.run_reset('--apply')
        self.assertTrue(sock.exists(), f'Replacement regular file deleted; exit={code}\n{out}')

    def test_safety_repo_wide_prune(self):
        _, wt, _ = self.make_task()
        unrelated = self.tmp / 'unrelated'
        sh('git', 'worktree', 'add', '-b', 'orchd/deadbeef', str(unrelated), 'main', cwd=self.repo)
        shutil.rmtree(unrelated)
        shutil.rmtree(wt)
        before = sh('git', 'worktree', 'list', '--porcelain', cwd=self.repo)
        self.assertIn(str(unrelated), before)
        code, out = self.run_reset('--apply')
        after = sh('git', 'worktree', 'list', '--porcelain', cwd=self.repo)
        self.assertIn(str(unrelated), after, f'Unrelated registration pruned; exit={code}\n{out}')

    def test_safety_null_resource_fields(self):
        tid, wt, _ = self.make_task()
        self.con.execute('UPDATE tasks SET branch=NULL, worktree=NULL, socket=NULL WHERE id=?', (tid,))
        code, out = self.run_reset('--apply')
        self.assertTrue(wt.exists(), f'Unattributed resource deleted; exit={code}\n{out}')

    def test_safety_wal_contents_are_read_not_ignored(self):
        # a writer still holds the DB open: a new closed task lives only in the WAL. It must be seen
        # (so the open task still blocks the reset) and no sidecar may be created or changed in home.
        _, wt, _ = self.make_task()
        self.make_task(status="running")
        names = sorted(p.name for p in self.home.iterdir())
        self.assertIn("orchd.db-wal", names)
        code, out = self.run_reset("--apply")
        self.assertEqual(code, 2, out)
        self.assertTrue(wt.exists())
        self.assertEqual(sorted(p.name for p in self.home.iterdir()), names)

    def test_safety_socket_connect_error_is_not_proof_of_death(self):
        _, wt, sdir = self.make_task()
        with patch.object(demo_reset.socket.socket, "connect", side_effect=PermissionError("denied")):
            code, out = self.run_reset("--apply")
        self.assertEqual(code, 1, out)
        self.assertTrue((sdir / "w.sock").exists())
        self.assertTrue(wt.exists())

    def test_safety_local_commit_after_plan_keeps_branch_and_worktree(self):
        tid, wt, _ = self.make_task()
        plan_task = demo_reset.Reset.plan_task
        def commit_after_plan(plan, task):
            plan_task(plan, task)
            (wt / "late.txt").write_text("late\n")
            sh("git", "add", ".", cwd=wt)
            sh("git", "commit", "-m", "late", cwd=wt)
        with patch.object(demo_reset.Reset, "plan_task", commit_after_plan):
            code, out = self.run_reset("--apply")
        self.assertEqual(code, 1, out)
        self.assertTrue((wt / "late.txt").exists())
        self.assertIn(f"orchd/{tid}", self.branches())

    # --- re-review boundaries: ledger barrier and resource identity ---

    def test_apply_on_idle_db_leaves_no_sidecars(self):
        self.make_task()
        self.con.close()
        before = sorted(p.name for p in self.home.iterdir())
        code, out = self.run_reset("--apply")
        self.assertEqual(code, 0, out)
        self.assertEqual(sorted(p.name for p in self.home.iterdir()), before)
        self.assertEqual(list(self.projects.joinpath(".orchd-worktrees").glob(".demo-reset-*")), [])

    def test_active_task_committed_after_snapshot_is_seen_under_the_lock(self):
        # no WAL at snapshot time; a writer then creates one and commits a running demo task
        _, wt, sdir = self.make_task()
        self.con.close()
        self.assertFalse((self.home / "orchd.db-wal").exists())
        load_tasks = demo_reset.load_tasks
        def dispatch_after_snapshot(home, repos):
            tasks = load_tasks(home, repos)
            writer = store.connect(self.home / "orchd.db")
            self.addCleanup(writer.close)
            store.create_task(writer, id="deadbeef", repo=REPO, repo_path=str(self.repo), title="t",
                              instructions="i", done_when="d", orch_thread="o", codex_bin="c", status="running")
            return tasks
        with patch.object(demo_reset, "load_tasks", dispatch_after_snapshot):
            code, out = self.run_reset("--apply")
        self.assertEqual(code, 2, out)
        self.assertIn("deadbeef (running)", out)
        self.assertTrue(wt.exists())
        self.assertTrue(sdir.exists())
        self.assertIn(f"orchd/00000001", self.branches())

    def test_writers_are_held_off_while_apply_acts(self):
        self.make_task()
        plan_task = demo_reset.Reset.plan_task
        blocked = []
        def try_write(plan, task):
            writer = sqlite3.connect(self.home / "orchd.db", timeout=0)
            try:
                writer.execute("UPDATE tasks SET status='running' WHERE id=?", (task["id"],))
                writer.commit()
            except sqlite3.OperationalError as e:
                blocked.append(str(e))
            finally:
                writer.close()
            plan_task(plan, task)
        with patch.object(demo_reset.Reset, "plan_task", try_write):
            code, out = self.run_reset("--apply")
        self.assertEqual(blocked, ["database is locked"], out)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.con.execute("SELECT status FROM tasks").fetchone()[0], "closed")

    def test_busy_db_refuses_to_run(self):
        _, wt, _ = self.make_task()
        self.con.execute("BEGIN IMMEDIATE")
        self.addCleanup(lambda: self.con.in_transaction and self.con.execute("ROLLBACK"))
        with patch.object(demo_reset, "LOCK_WAIT_S", 0):
            code, out = self.run_reset("--apply")
        self.assertEqual(code, 2, out)
        self.assertIn("write lock", out)
        self.assertTrue(wt.exists())

    def test_lock_budget_stops_starting_new_chains(self):
        _, wt, sdir = self.make_task()
        with patch.object(demo_reset, "LOCK_BUDGET_S", -1):
            code, out = self.run_reset("--apply")
        self.assertEqual(code, 1, out)
        self.assertIn("not attempted: the DB write lock was held", out)
        self.assertTrue(wt.exists())
        self.assertTrue(sdir.exists())

    def _after_plan(self, fn):
        plan_task = demo_reset.Reset.plan_task
        done = []
        def wrapped(plan, task):
            plan_task(plan, task)
            if not done:  # once, after the first task is planned
                done.append(fn())
        return patch.object(demo_reset.Reset, "plan_task", wrapped)

    def test_remote_replaced_after_plan_is_kept(self):
        for kind in ("symlink", "directory"):
            with self.subTest(kind):
                tid, wt, _ = self.make_task()
                foreign = self.tmp / f"foreign-{kind}.git"
                sh("git", "clone", "--bare", str(self.remote), str(foreign))
                original = self.tmp / f"original-{kind}.git"
                def swap():
                    self.remote.rename(original)
                    if kind == "symlink":
                        self.remote.symlink_to(foreign, target_is_directory=True)
                    else:
                        foreign.rename(self.remote)
                with self._after_plan(swap):
                    code, out = self.run_reset("--apply")
                self.assertEqual(code, 1, out)
                self.assertIn("remote", out)
                self.assertIn(f"refs/heads/orchd/{tid}", sh("git", "ls-remote", str(self.remote)))
                self.assertIn(f"refs/heads/orchd/{tid}", sh("git", "ls-remote", str(original)))
                self.assertTrue(wt.exists())
                self.assertIn(f"orchd/{tid}", self.branches())
                if self.remote.is_symlink():
                    self.remote.unlink()
                else:
                    shutil.rmtree(self.remote)
                original.rename(self.remote)

    def test_worktree_replaced_after_plan_is_kept(self):
        tid, wt, _ = self.make_task()
        moved = self.tmp / "owned-moved"
        def swap():
            sh("git", "worktree", "move", str(wt), str(moved), cwd=self.repo)
            sh("git", "worktree", "add", "-b", "foreign", str(wt), "main", cwd=self.repo)
        with self._after_plan(swap):
            code, out = self.run_reset("--apply")
        self.assertEqual(code, 1, out)
        self.assertTrue(moved.exists())
        self.assertEqual(sh("git", "rev-parse", "--abbrev-ref", "HEAD", cwd=wt), "foreign")
        self.assertIn(f"orchd/{tid}", self.branches())

    def test_worktree_swapped_at_move_time_is_moved_back(self):
        # the swap lands after every check, right before `git worktree move`: the quarantine catches it
        tid, wt, _ = self.make_task()
        moved = self.tmp / "owned-moved"
        mkdtemp = demo_reset.tempfile.mkdtemp
        def swap_then_mkdtemp(*a, **kw):
            if kw.get("dir") != wt.parent:  # only the quarantine, not the DB snapshot's temp dir
                return mkdtemp(*a, **kw)
            sh("git", "worktree", "move", str(wt), str(moved), cwd=self.repo)
            sh("git", "worktree", "add", "-b", "foreign", str(wt), "main", cwd=self.repo)
            (wt / "foreign.txt").write_text("keep\n")
            sh("git", "add", ".", cwd=wt)
            sh("git", "commit", "-m", "foreign", cwd=wt)
            return mkdtemp(*a, **kw)
        with patch.object(demo_reset.tempfile, "mkdtemp", swap_then_mkdtemp):
            code, out = self.run_reset("--apply")
        self.assertEqual(code, 1, out)
        self.assertIn("moved back", out)
        self.assertEqual((wt / "foreign.txt").read_text(), "keep\n")
        self.assertEqual(sh("git", "rev-parse", "--abbrev-ref", "HEAD", cwd=wt), "foreign")
        self.assertTrue(moved.exists())
        self.assertIn(f"orchd/{tid}", self.branches())
        self.assertEqual(list(wt.parent.glob(".demo-reset-*")), [])

    def test_dirty_at_remove_time_is_moved_back_to_its_path(self):
        tid, wt, _ = self.make_task()
        with self._after_plan(lambda: (wt / "late-untracked.txt").write_text("keep\n")):
            code, out = self.run_reset("--apply")
        self.assertEqual(code, 1, out)
        self.assertIn("moved back", out)
        self.assertEqual((wt / "late-untracked.txt").read_text(), "keep\n")
        self.assertIn(f"orchd/{tid}", self.branches())
        self.assertEqual(list(wt.parent.glob(".demo-reset-*")), [])

    def test_symlinked_projects_or_remotes_root_is_refused(self):
        for root in ("projects", "remotes"):
            with self.subTest(root):
                tid, wt, _ = self.make_task()
                path = getattr(self, root)
                physical = self.tmp / f"physical-{root}"
                path.rename(physical)
                path.symlink_to(physical, target_is_directory=True)
                try:
                    code, out = self.run_reset("--apply")
                finally:
                    path.unlink()
                    physical.rename(path)
                self.assertEqual(code, 1, out)
                self.assertIn("passes through a symlink", out)
                self.assertTrue(wt.exists())
                self.assertIn(f"orchd/{tid}", self.branches())
                self.assertIn(f"refs/heads/orchd/{tid}", self.remote_branches())


if __name__ == "__main__":
    unittest.main()
