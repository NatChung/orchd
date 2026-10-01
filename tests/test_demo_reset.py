"""scripts/demo-reset.py against a scratch ORCHD_HOME / projects / remotes / socket root. Nothing real is touched."""
import importlib.util
import io
import os
import socket
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

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
        self.tmp = Path(tempfile.mkdtemp(prefix="demoreset-")).resolve()
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


if __name__ == "__main__":
    unittest.main()
