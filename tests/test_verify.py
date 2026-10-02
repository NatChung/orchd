"""Issue #4: hash-locked verify commands rerun by an independent worker at the locked SHA."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from orchd import core, store, verify
from tests.test_orchd import FakeRuntime

CLI = Path(__file__).resolve().parents[1] / "bin" / "orchd"
GIT_ENV = dict(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class VerifyTest(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, GIT_ENV)
        env.start()
        self.addCleanup(env.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.home = self.tmp / "home"
        self.con = store.connect(self.home / "orchd.db")
        self.addCleanup(self.con.close)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "tests").mkdir()
        (self.repo / "tests" / "check.sh").write_text("test -f impl.txt\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "verify script first")
        self.wa, self.wv = self.tmp / "wt-A", self.tmp / "wt-V"
        git(self.repo, "worktree", "add", "-q", "-b", "orchd/A", str(self.wa), "main")
        git(self.repo, "worktree", "add", "-q", "-b", "orchd/V", str(self.wv), "main")
        self.commit(self.wa, "impl.txt", "done\n")
        self.a = self.task("A", self.wa, status="done", verify="sh tests/check.sh")
        self.v = self.task("V", self.wv, verifies="A")

    def commit(self, cwd, name, text, message="c"):
        path = Path(cwd) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        git(cwd, "add", "-A")
        git(cwd, "commit", "-q", "-m", message)
        return git(cwd, "rev-parse", "HEAD")

    def task(self, tid, worktree, status="acked", **extra):
        fields = dict(id=tid, repo="repo", repo_path=str(self.repo), title=tid, instructions="i", done_when="d",
                      orch_thread="thread-A", codex_bin="/fake/codex", branch=f"orchd/{tid}", worktree=str(worktree),
                      session_id=f"session-{tid}", job_id=f"job-{tid}", model="claude-sonnet-5-5", status=status)
        return store.create_task(self.con, **dict(fields, **extra))

    def lock(self, paths=("tests/check.sh",), command=None, task="A"):
        return verify.lock(self.con, task, list(paths), command, orch_thread="thread-A")

    def branch(self, cwd):
        return subprocess.run(["git", "symbolic-ref", "-q", "--short", "HEAD"], cwd=cwd,
                              capture_output=True, text=True).stdout.strip()

    # --- the lock ---------------------------------------------------------------------------------------

    def test_lock_records_head_object_hashes_and_command_without_touching_the_author(self):
        head = git(self.wa, "rev-parse", "HEAD")
        lock = self.lock(["tests/check.sh", "tests"])
        self.assertEqual(lock["sha"], head)
        self.assertEqual(lock["paths"]["tests/check.sh"], git(self.wa, "rev-parse", f"{head}:tests/check.sh"))
        self.assertEqual(lock["paths"]["tests"], git(self.wa, "rev-parse", f"{head}:tests"))
        self.assertEqual(lock["command"], "sh tests/check.sh")  # dispatch's verify is the default
        self.assertEqual((git(self.wa, "rev-parse", "HEAD"), git(self.wa, "status", "--porcelain")), (head, ""))
        self.assertEqual(self.branch(self.wa), "orchd/A")
        row = self.con.execute("SELECT kind FROM messages WHERE task_id='A'").fetchone()
        self.assertEqual(row["kind"], verify.LOCK)  # event log, not an inbox kind
        self.assertNotIn(verify.LOCK, store.ORCH_KINDS)

    def test_lock_refuses_paths_that_escape_or_are_not_plain_tracked_files(self):
        bad = ["../repo/tests/check.sh", "/etc/passwd", "tests/../impl.txt", "tests/./check.sh", "./impl.txt",
               "tests//check.sh", "", ".git/config", "tests/.git", "missing.txt", "tests/missing.sh", "a\\b"]
        for path in bad:
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.lock([path])
        with self.assertRaises(ValueError):
            self.lock([])
        with self.assertRaises(ValueError):
            self.lock("tests/check.sh")  # a bare string is not a list of paths
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)

    def test_lock_refuses_symlinks_at_the_path_its_parents_or_beneath_a_dir(self):
        os.symlink("tests", self.wa / "link")
        os.symlink("/etc/hosts", self.wa / "tests" / "outside")
        git(self.wa, "add", "-A")
        git(self.wa, "commit", "-q", "-m", "links")
        for path in ("link", "link/check.sh", "tests/outside", "tests"):
            with self.subTest(path=path), self.assertRaises(ValueError) as caught:
                self.lock([path])
            self.assertIn("symlink", str(caught.exception))
        self.assertEqual(self.lock(["impl.txt"])["paths"].keys(), {"impl.txt"})  # a plain file still locks

    def test_lock_refuses_dirty_or_untracked_work_closed_tasks_verifiers_and_missing_command(self):
        (self.wa / "scratch.txt").write_text("not committed")
        with self.assertRaisesRegex(ValueError, "uncommitted or untracked"):
            self.lock()
        os.remove(self.wa / "scratch.txt")
        with self.assertRaisesRegex(ValueError, "is a verifier"):
            self.lock(task="V")
        self.task("B", self.wa)
        with self.assertRaisesRegex(ValueError, "needs a command"):
            self.lock(task="B")
        store.update_task(self.con, "A", status="closed")
        with self.assertRaisesRegex(ValueError, "closed"):
            self.lock()

    def test_relock_reports_locked_paths_that_changed_since_the_previous_lock(self):
        self.lock(["tests/check.sh", "impl.txt"])
        self.commit(self.wa, "tests/check.sh", "true\n", "weaken the test")
        again = self.lock(["tests/check.sh", "impl.txt"])
        self.assertEqual(again["changed_paths"], ["tests/check.sh"])
        self.assertFalse(again["command_changed"])
        self.assertTrue(self.lock(["tests/check.sh"], command="true")["command_changed"])

    # --- the independent rerun --------------------------------------------------------------------------

    def test_independent_worker_passes_at_the_locked_sha_and_leaves_both_trees_as_they_were(self):
        lock = self.lock()
        a_head = git(self.wa, "rev-parse", "HEAD")
        result = verify.run(self.con, "V")
        self.assertTrue(result["passed"])
        self.assertEqual((result["exit"], result["hash_ok"], result["dirty"]), (0, True, False))
        self.assertEqual((result["sha"], result["head_after"], result["verifier"]), (lock["sha"], lock["sha"], "V"))
        self.assertTrue(result["restored"])
        self.assertIs(result["cleanup"], True)
        self.assertEqual(self.branch(self.wv), "orchd/V")
        self.assertEqual((git(self.wa, "rev-parse", "HEAD"), git(self.wa, "status", "--porcelain")), (a_head, ""))
        st = verify.status(self.con, "A")
        self.assertEqual((st["state"], st["author"], st["verifier"], st["lock_sha"], st["verified_sha"],
                          st["current_sha"]), ("pass", "A", "V", lock["sha"], lock["sha"], lock["sha"]))
        self.assertEqual(verify.status(self.con, "V")["role"], "verifier")
        self.assertEqual(verify.status(self.con, "V")["state"], "pass")

    def test_verify_refuses_before_any_checkout(self):
        cases = []
        self.task("N", self.wv)  # dispatched without verifies
        cases.append(("N", "not dispatched to verify"))
        self.task("S", self.wv, verifies="S")
        cases.append(("S", "cannot verify itself"))
        for tid, why in cases:
            with self.subTest(tid=tid), self.assertRaisesRegex(ValueError, why):
                verify.run(self.con, tid)
        with self.assertRaisesRegex(ValueError, "no lock"):
            verify.run(self.con, "V")
        self.lock()
        self.task("W", self.wv, verifies="A", session_id="session-A")
        with self.assertRaisesRegex(ValueError, "same worker session"):
            verify.run(self.con, "W")
        (self.wv / "local.txt").write_text("verifier's own unsaved work")
        with self.assertRaisesRegex(ValueError, "not clean"):
            verify.run(self.con, "V")
        self.assertEqual((self.wv / "local.txt").read_text(), "verifier's own unsaved work")
        os.remove(self.wv / "local.txt")
        store.update_task(self.con, "A", status="closed")
        with self.assertRaisesRegex(ValueError, "author task A is closed"):
            verify.run(self.con, "V")
        self.assertEqual(self.branch(self.wv), "orchd/V")
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM messages WHERE kind=?", (verify.RESULT,)).fetchone()[0], 0)

    def test_author_report_alone_is_not_acceptance(self):
        core.report(self.con, FakeRuntime(), "A", "done", "all tests pass", "python -m unittest: OK")
        self.assertEqual(verify.status(self.con, "A")["state"], "none")
        self.lock()
        self.assertEqual(verify.status(self.con, "A")["state"], "locked")

    def test_failing_command_is_recorded_as_fail(self):
        self.lock(command="echo boom; exit 3")
        result = verify.run(self.con, "V")
        self.assertEqual((result["passed"], result["exit"]), (False, 3))
        self.assertIn("boom", result["tail"])
        st = verify.status(self.con, "A")
        self.assertEqual((st["state"], st["exit"]), ("fail", 3))

    def test_command_that_modifies_the_tree_fails_as_dirty_and_its_changes_are_kept(self):
        self.lock(command="echo weakened >> tests/check.sh; touch new-output.txt")
        result = verify.run(self.con, "V")
        self.assertEqual((result["exit"], result["dirty"], result["passed"], result["restored"]), (0, True, False, False))
        self.assertIn("weakened", (self.wv / "tests" / "check.sh").read_text())  # nothing thrown away
        self.assertTrue((self.wv / "new-output.txt").exists())
        self.assertEqual(verify.status(self.con, "A")["state"], "fail")

    def test_hash_mismatch_against_the_lock_fails(self):
        # At the locked SHA the hashes can only differ if the lock row itself is wrong, so forge one.
        lock = self.lock()
        forged = dict(sha=lock["sha"], paths={"tests/check.sh": "0" * 40}, command="sh tests/check.sh", orch_thread=None)
        store.add_message(self.con, "A", verify.LOCK, "forged", json.dumps(forged))
        result = verify.run(self.con, "V")
        self.assertEqual((result["exit"], result["hash_ok"], result["passed"]), (0, False, False))
        self.assertEqual(verify.status(self.con, "A")["state"], "fail")

    def test_command_timeout_is_a_fail(self):
        self.lock(command="sleep 5")
        result = verify.run(self.con, "V", timeout=0.3)
        self.assertEqual((result["exit"], result["passed"]), (None, False))
        self.assertIn("timed out", result["tail"])

    # --- the command's processes: cleaned up before dirty/head/restore -----------------------------------

    def late_writer(self, then, delay=1):
        """A background child that outlives the shell and writes late.txt into the verifier tree `delay` s later.

        The shell waits until the child has written its pid, so the child is really running when the shell
        moves on; its output goes to /dev/null, so nothing holds orchd's capture open."""
        pidfile = self.tmp / "writer.pid"
        command = (f"sh -c 'echo $$ > {pidfile}; sleep {delay}; echo leaked > late.txt' >/dev/null 2>&1 & "
                   f"while [ ! -s {pidfile} ]; do sleep 0.01; done; {then}")
        return command, pidfile

    def assert_writer_gone_and_nothing_landed(self, pidfile, delay=1):
        pid = int(pidfile.read_text())
        deadline = time.monotonic() + delay + 2  # well past the writer's delay
        while time.monotonic() < deadline:
            self.assertFalse((self.wv / "late.txt").exists(), "a child wrote into the tree after verify returned")
            time.sleep(0.05)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        self.assertEqual(git(self.wv, "status", "--porcelain", "--untracked-files=all"), "")
        self.assertEqual(self.branch(self.wv), "orchd/V")

    def test_timeout_kills_background_children_before_the_restore(self):
        command, pidfile = self.late_writer("sleep 30")
        self.lock(command=command)
        started = time.monotonic()
        result = verify.run(self.con, "V", timeout=0.3)
        self.assertLess(time.monotonic() - started, 5)
        self.assert_writer_gone_and_nothing_landed(pidfile)
        self.assertEqual((result["exit"], result["passed"], result["cleanup"], result["dirty"], result["restored"]),
                         (None, False, True, False, True))

    def test_background_writer_of_a_passing_command_is_killed_before_the_tree_is_judged(self):
        command, pidfile = self.late_writer("exit 0")
        lock = self.lock(command=command)
        result = verify.run(self.con, "V")
        self.assert_writer_gone_and_nothing_landed(pidfile)
        self.assertEqual((result["exit"], result["cleanup"], result["dirty"], result["restored"]), (0, True, False, True))
        self.assertTrue(result["passed"])  # the write never lands, so the pass describes the tree that stays
        self.assertEqual(verify.status(self.con, "A")["state"], "pass")
        self.assertEqual(verify.status(self.con, "A")["lock_sha"], lock["sha"])

    def test_background_writer_of_a_failing_command_is_killed_too(self):
        command, pidfile = self.late_writer("exit 5")
        self.lock(command=command)
        result = verify.run(self.con, "V")
        self.assert_writer_gone_and_nothing_landed(pidfile)
        self.assertEqual((result["exit"], result["passed"], result["cleanup"], result["dirty"]), (5, False, True, False))

    def test_cli_timeout_leaves_no_late_write_in_the_verifier_tree(self):
        command, pidfile = self.late_writer("sleep 30", delay=2)  # the review's repro: writes 2 s in, timeout 1 s
        self.lock(command=command)
        env = dict(os.environ, ORCHD_HOME=str(self.home))
        done = subprocess.run([sys.executable, str(CLI), "verify", "V", "--timeout", "1"],
                              capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assert_writer_gone_and_nothing_landed(pidfile, delay=2)
        self.assertEqual(json.loads(done.stdout)["cleanup"], True)

    def test_unconfirmed_cleanup_is_a_fail_and_the_tree_stays_at_the_locked_sha(self):
        # No test process can be made unkillable, so pretend the group never empties.
        lock = self.lock(command="true")
        with mock.patch.object(verify.os, "killpg", lambda pgid, sig: None), \
                mock.patch.object(verify, "CLEANUP_WAIT", 0.2):
            result = verify.run(self.con, "V")
        self.assertEqual((result["exit"], result["cleanup"], result["passed"], result["restored"]), (0, False, False, False))
        self.assertIsNone(result["dirty"])  # not judged while a process might still write
        self.assertIn("not confirmed", result["restore_error"])
        self.assertEqual(git(self.wv, "rev-parse", "HEAD"), lock["sha"])
        self.assertEqual(verify.status(self.con, "A")["state"], "fail")

    def test_an_exception_after_the_checkout_restores_a_clean_tree_and_is_recorded(self):
        self.lock()
        real, calls = verify._dirty, []

        def dirty(cwd):
            calls.append(cwd)
            if len(calls) == 2:  # 1: the pre-check; 2: judging the tree after the command
                raise subprocess.CalledProcessError(128, ["git", "status"])
            return real(cwd)
        with mock.patch.object(verify, "_dirty", dirty), self.assertRaises(subprocess.CalledProcessError):
            verify.run(self.con, "V")
        self.assertEqual(self.branch(self.wv), "orchd/V")
        row = json.loads(self.con.execute("SELECT evidence FROM messages WHERE kind=?", (verify.RESULT,)).fetchone()[0])
        self.assertIn("CalledProcessError", row["error"])
        self.assertEqual((row["passed"], row["restored"]), (False, True))
        self.assertEqual(verify.status(self.con, "A")["state"], "fail")

    def test_an_exception_with_a_dirty_tree_keeps_the_files_and_does_not_restore(self):
        lock = self.lock(command="echo mine > output.txt")
        real, calls = verify._dirty, []

        def dirty(cwd):
            calls.append(cwd)
            if len(calls) == 2:
                raise RuntimeError("status broke")
            return real(cwd)
        with mock.patch.object(verify, "_dirty", dirty), self.assertRaisesRegex(RuntimeError, "status broke"):
            verify.run(self.con, "V")
        self.assertEqual((self.wv / "output.txt").read_text(), "mine\n")
        self.assertEqual(git(self.wv, "rev-parse", "HEAD"), lock["sha"])  # no forced checkout
        row = json.loads(self.con.execute("SELECT evidence FROM messages WHERE kind=?", (verify.RESULT,)).fetchone()[0])
        self.assertEqual((row["passed"], row["restored"]), (False, False))

    def test_a_failed_restore_is_recorded_and_returned(self):
        # git refuses to switch while the worktree's index is locked; status still works.
        self.lock(command='touch "$(git rev-parse --git-path index.lock)"')
        result = verify.run(self.con, "V")
        self.assertEqual((result["cleanup"], result["dirty"], result["restored"]), (True, False, False))
        self.assertIn("index.lock", result["restore_error"])
        row = json.loads(self.con.execute("SELECT evidence FROM messages WHERE kind=?", (verify.RESULT,)).fetchone()[0])
        self.assertEqual((row["restored"], row["restore_error"]), (False, result["restore_error"]))

    def test_new_author_commit_makes_a_pass_stale_until_relocked_and_rerun(self):
        self.lock()
        self.assertTrue(verify.run(self.con, "V")["passed"])
        new = self.commit(self.wa, "more.txt", "later\n")
        st = verify.status(self.con, "A")
        self.assertEqual((st["state"], st["current_sha"]), ("stale", new))
        self.lock()
        self.assertEqual(verify.status(self.con, "A")["state"], "locked")  # the old pass does not carry over
        self.assertTrue(verify.run(self.con, "V")["passed"])
        self.assertEqual(verify.status(self.con, "A")["state"], "pass")

    def test_stale_tracks_the_branch_even_after_the_author_worktree_is_gone(self):
        self.lock()
        verify.run(self.con, "V")
        git(self.repo, "worktree", "remove", "--force", str(self.wa))
        self.assertEqual(verify.status(self.con, "A")["state"], "pass")
        git(self.repo, "update-ref", "refs/heads/orchd/A", git(self.repo, "rev-parse", "main"))
        self.assertEqual(verify.status(self.con, "A")["state"], "stale")

    def test_recorded_passed_flag_is_not_trusted(self):
        self.lock(command="exit 1")
        verify.run(self.con, "V")
        row = self.con.execute("SELECT id, evidence FROM messages WHERE kind=?", (verify.RESULT,)).fetchone()
        record = dict(json.loads(row["evidence"]), passed=True)
        self.con.execute("UPDATE messages SET evidence=? WHERE id=?", (json.dumps(record), row["id"]))
        self.assertEqual(verify.status(self.con, "A")["state"], "fail")

    # --- surfaces ---------------------------------------------------------------------------------------

    def test_inbox_shows_verification_and_skips_git_for_tasks_without_a_lock(self):
        rt = FakeRuntime()
        self.lock()
        verify.run(self.con, "V")
        core.report(self.con, rt, "V", "done", "verified", "")
        core.report(self.con, rt, "A", "done", "impl", "")
        self.task("P", self.tmp / "no-such-dir")  # would fail any git call
        core.progress(self.con, rt, "P", "plain task")
        by_task = {m["task_id"]: m["verification"] for m in core.inbox(self.con, "thread-A")}
        self.assertEqual(by_task["P"], dict(state="none"))
        self.assertEqual((by_task["A"]["state"], by_task["A"]["verifier"]), ("pass", "V"))
        self.assertEqual((by_task["V"]["state"], by_task["V"]["role"]), ("pass", "verifier"))

    def test_inbox_keeps_every_message_when_a_verification_lookup_breaks(self):
        rt = FakeRuntime()
        self.lock()
        core.report(self.con, rt, "A", "done", "impl", "")
        core.progress(self.con, rt, "V", "verifier note")
        self.con.execute("UPDATE tasks SET repo_path=?, worktree=? WHERE id='A'",
                         (str(self.tmp / "gone-repo"), str(self.tmp / "gone-wt")))
        self.con.execute("UPDATE tasks SET verifies='deleted' WHERE id='V'")
        by_task = {m["task_id"]: m["verification"] for m in core.inbox(self.con, "thread-A")}
        self.assertEqual(by_task["A"]["state"], "stale")  # current SHA unknown is not a pass
        self.assertEqual(by_task["V"], dict(state="error", error="KeyError"))
        self.assertEqual(core.inbox(self.con, "thread-A"), [])

    def test_dispatch_stores_the_spec_and_validates_verifies(self):
        rt = FakeRuntime()
        kw = dict(orch_thread="thread-A", repo="demo", title="t", instructions="i", done_when="d",
                  model_reason="clear scope", task_type="code")
        with self.assertRaisesRegex(ValueError, "unknown task"):
            core.dispatch(self.con, rt, verifies="nope", **kw)
        t = core.dispatch(self.con, rt, verify="make test", manual_checks="look at the page", verifies="A", **kw)
        self.assertEqual((t["verify"], t["manual_checks"], t["verifies"]), ("make test", "look at the page", "A"))
        event = json.loads(self.con.execute("SELECT body FROM messages WHERE task_id=? AND kind='dispatch'",
                                            (t["id"],)).fetchone()[0])
        self.assertEqual((event["verify"], event["verifies"]), ("make test", "A"))
        plain = core.dispatch(self.con, rt, **kw)
        event = json.loads(self.con.execute("SELECT body FROM messages WHERE task_id=? AND kind='dispatch'",
                                            (plain["id"],)).fetchone()[0])
        self.assertNotIn("verify", event)  # unchanged payload for ordinary dispatches
        store.update_task(self.con, "A", status="closed")
        with self.assertRaisesRegex(ValueError, "is closed"):
            core.dispatch(self.con, rt, verifies="A", **kw)

    def test_cli_exit_codes_pass_fail_refused(self):
        env = dict(os.environ, ORCHD_HOME=str(self.home))
        run = lambda: subprocess.run([sys.executable, str(CLI), "verify", "V"], capture_output=True, text=True, env=env)
        refused = run()
        self.assertEqual(refused.returncode, 2, refused.stderr)
        self.assertIn("no lock", refused.stderr)
        self.lock()
        passed = run()
        self.assertEqual(passed.returncode, 0, passed.stderr)
        self.assertTrue(json.loads(passed.stdout)["passed"])
        self.lock(command="exit 4")
        self.assertEqual(run().returncode, 1)


if __name__ == "__main__":
    unittest.main()
