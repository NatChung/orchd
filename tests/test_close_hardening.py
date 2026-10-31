"""PR #101 review fixes for issue #96: close never deletes ignored files, an unreadable pid blocks instead of
counting as gone, a Codex stop signals only verified processes, `stuck-close` touches no file, and the runbook's
manual cleanup is a full checklist. Real git, real processes and temp dirs; fake data only."""
import hashlib
import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

import psutil

from orchd import core, processes, store
from orchd.runtime import Runtime
from tests.test_close_recovery import HookRuntime, git

ROOT = Path(__file__).resolve().parents[1]


class WorktreeCase(unittest.TestCase):
    """A plain repo (no submodule) with a pushed main and one task worktree."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        remote = root / "remote.git"
        git("init", "-q", "--bare", "-b", "main", str(remote), cwd=root)
        self.projects = root / "projects"
        repo = self.projects / "demo"
        repo.mkdir(parents=True)
        git("init", "-q", "-b", "main", cwd=repo)
        (repo / ".gitignore").write_text("*.env\n.venv/\n__pycache__/\nnode_modules/\nout/\n")
        git("add", ".gitignore", cwd=repo)
        git("commit", "-q", "-m", "i", cwd=repo)
        git("remote", "add", "origin", str(remote), cwd=repo)
        git("push", "-q", "-u", "origin", "main", cwd=repo)
        git("remote", "set-head", "origin", "main", cwd=repo)
        os.environ["ORCHD_PROJECTS"] = str(self.projects)
        self.rt = HookRuntime()
        self.repo = self.rt.repo_path("demo")
        self.base, self.branch, self.wt = self.rt.create_worktree(self.repo, "demo", "abcd1234")
        self.con = store.connect(root / "t.db")
        store.create_task(self.con, id="abcd1234", repo="demo", repo_path=str(self.repo), title="T",
                          instructions="i", done_when="d", orch_thread="th", codex_bin="c", model="claude-sonnet-5-5",
                          base=self.base, branch=self.branch, worktree=self.wt, status="done")

    def tearDown(self):
        os.environ.pop("ORCHD_PROJECTS", None)
        self.tmp.cleanup()

    def put(self, relative, text="x"):
        path = Path(self.wt, relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path


class IgnoredFilesTest(WorktreeCase):
    def test_ignored_file_makes_the_worktree_unremovable_and_is_named(self):
        secret = self.put("local.env", "TOKEN=fake")
        removable, reason = self.rt.worktree_state(self.wt, self.base)
        self.assertFalse(removable)
        self.assertIn("ignored", reason)
        self.assertIn("local.env", reason)
        self.assertEqual(secret.read_text(), "TOKEN=fake")

    def test_close_keeps_the_worktree_with_an_ignored_file_and_reports_it(self):
        secret = self.put("local.env", "TOKEN=fake")
        result = core.close(self.con, self.rt, "abcd1234")
        task = store.get_task(self.con, "abcd1234")
        self.assertEqual(task["status"], "closed")
        self.assertIn("local.env", result["worktree"])
        self.assertIn("local.env", task["note"])
        self.assertEqual(secret.read_text(), "TOKEN=fake")

    def test_ignored_file_that_appears_after_the_first_check_is_still_kept(self):
        late = Path(self.wt, "late.env")
        self.rt.after_state = lambda: late.write_text("late")
        result = core.close(self.con, self.rt, "abcd1234")
        self.assertIn("late.env", result["worktree"])
        self.assertTrue(late.exists())

    def test_ignored_file_inside_an_ignored_directory_is_kept(self):
        data = self.put("out/result.bin", "valuable")
        removable, reason = self.rt.worktree_state(self.wt, self.base)
        self.assertFalse(removable)
        self.assertIn("out/", reason)
        self.assertTrue(data.exists())

    def test_remove_without_a_base_still_checks_ignored_files(self):
        """The dispatch-failure path calls remove_worktree(repo, worktree) with no base."""
        secret = self.put("local.env", "TOKEN=fake")
        kept = self.rt.remove_worktree(self.repo, self.wt)
        self.assertIn("local.env", kept)
        self.assertTrue(secret.exists())

    def test_only_rebuildable_ignored_directories_do_not_block_removal(self):
        for name in (".venv/bin/python", "node_modules/pkg/index.js", "pkg/__pycache__/m.cpython-313.pyc"):
            self.put(name)
        removable, reason = self.rt.worktree_state(self.wt, self.base)
        self.assertTrue(removable, reason)
        self.assertIsNone(self.rt.remove_worktree(self.repo, self.wt, self.base))
        self.assertFalse(Path(self.wt).exists())

    def test_rebuildable_directory_next_to_a_real_ignored_file_still_blocks(self):
        self.put(".venv/bin/python")
        self.put("keep.env")
        removable, reason = self.rt.worktree_state(self.wt, self.base)
        self.assertFalse(removable)
        self.assertIn("keep.env", reason)
        self.assertNotIn(".venv", reason)

    def test_rebuildable_names_as_regular_files_or_symlinks_are_kept(self):
        from orchd.runtime import REBUILDABLE_IGNORED
        # Ignore the name regardless of type, unlike the fixture's directory-only patterns.
        exclude = Path(git("rev-parse", "--git-path", "info/exclude", cwd=Path(self.wt)).strip())
        if not exclude.is_absolute():
            exclude = Path(self.wt) / exclude
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text("\n".join(REBUILDABLE_IGNORED) + "\n")
        for name in REBUILDABLE_IGNORED:
            for kind in ("file", "symlink"):
                with self.subTest(name=name, kind=kind):
                    path = Path(self.wt, name)
                    if kind == "file":
                        path.write_text("unique data")
                    else:
                        path.symlink_to(self.repo, target_is_directory=True)
                    try:
                        removable, reason = self.rt.worktree_state(self.wt, self.base)
                        self.assertFalse(removable, reason)
                        self.assertIn(name, reason)
                        self.assertIn(name, self.rt.remove_worktree(self.repo, self.wt, self.base))
                        self.assertTrue(path.is_symlink() or path.is_file())
                    finally:
                        path.unlink()

    def test_safe_list_is_explicit(self):
        from orchd import runtime
        self.assertEqual(set(runtime.REBUILDABLE_IGNORED),
                         {".venv", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"})

    def test_ignored_file_in_a_submodule_is_a_loss(self):
        sub = Path(self.tmp.name, "sub")
        sub.mkdir()
        git("init", "-q", "-b", "main", cwd=sub)
        (sub / ".gitignore").write_text("*.env\n")
        git("add", ".gitignore", cwd=sub)
        git("commit", "-q", "-m", "s", cwd=sub)
        (sub / "secret.env").write_text("x")
        reason = self.rt._submodule_unsaved(str(sub), "vendor")
        self.assertIn("secret.env", reason or "")


class PidUnknownTest(unittest.TestCase):
    def test_permission_denied_pid_probe_is_alive_not_dead(self):
        rt = Runtime()
        with mock.patch("orchd.runtime.os.waitpid", side_effect=ChildProcessError), \
                mock.patch("orchd.runtime.os.kill", side_effect=PermissionError(1, "Operation not permitted")):
            self.assertTrue(rt.pid_alive(4242))

    def test_only_a_missing_pid_is_dead(self):
        rt = Runtime()
        with mock.patch("orchd.runtime.os.waitpid", side_effect=ChildProcessError), \
                mock.patch("orchd.runtime.os.kill", side_effect=ProcessLookupError(3, "No such process")):
            self.assertFalse(rt.pid_alive(4242))
        with mock.patch("orchd.runtime.os.waitpid", side_effect=ChildProcessError), \
                mock.patch("orchd.runtime.os.kill", side_effect=OSError(5, "weird")):
            self.assertTrue(rt.pid_alive(4242))

    def process(self, cwd="/elsewhere"):
        proc = mock.Mock(spec=psutil.Process, pid=987654)
        proc.uids.return_value = mock.Mock(real=os.getuid())
        proc.environ.side_effect = psutil.AccessDenied(987654)
        if isinstance(cwd, Exception):
            proc.cwd.side_effect = cwd
        else:
            proc.cwd.return_value = cwd
        return proc

    def test_process_with_unreadable_environ_and_cwd_blocks(self):
        proc = self.process(psutil.AccessDenied(987654))
        with mock.patch.object(processes.psutil, "process_iter", return_value=[proc]):
            with self.assertRaisesRegex(RuntimeError, "cannot inspect process 987654"):
                processes.task_processes("/wt/demo-t1")

    def test_process_with_unreadable_environ_blocks_even_when_cwd_is_elsewhere(self):
        proc = self.process("/elsewhere")
        with mock.patch.object(processes.psutil, "process_iter", return_value=[proc]):
            with self.assertRaisesRegex(RuntimeError, "cannot inspect process 987654"):
                processes.task_processes("/wt/demo-t1")

    def test_process_with_unreadable_uid_blocks(self):
        proc = self.process()
        proc.uids.side_effect = psutil.AccessDenied(987654)
        with mock.patch.object(processes.psutil, "process_iter", return_value=[proc]):
            with self.assertRaisesRegex(RuntimeError, "cannot inspect process 987654"):
                processes.task_processes("/wt/demo-t1")


class VanishingProcessTest(unittest.TestCase):
    """psutil raises SystemError, not NoSuchProcess, when a process exits while its environment is read."""

    def process(self, running):
        proc = mock.Mock(spec=psutil.Process, pid=987654)
        proc.uids.return_value = mock.Mock(real=os.getuid())
        proc.environ.side_effect = SystemError("returned a result with an exception set")
        proc.is_running.return_value = running
        return proc

    def test_transient_systemerror_is_retried(self):
        proc = self.process(True)
        proc.environ.side_effect = [SystemError("x"), {"ORCHD_WORKTREE": "/other"}]
        with mock.patch.object(processes.psutil, "process_iter", return_value=[proc]):
            self.assertEqual(processes.task_processes("/wt/demo-t1"), {})

    def test_systemerror_from_a_process_that_is_gone_means_gone(self):
        with mock.patch.object(processes.psutil, "process_iter", return_value=[self.process(False)]):
            self.assertEqual(processes.task_processes("/wt/demo-t1"), {})

    def test_systemerror_from_a_process_that_is_still_there_is_unknown_and_blocks(self):
        with mock.patch.object(processes.psutil, "process_iter", return_value=[self.process(True)]):
            with self.assertRaisesRegex(RuntimeError, "cannot inspect process 987654"):
                processes.task_processes("/wt/demo-t1")

    def test_systemerror_outside_the_environ_read_is_handled_the_same_way(self):
        proc = self.process(False)
        proc.uids.side_effect = SystemError("x")
        with mock.patch.object(processes.psutil, "process_iter", return_value=[proc]):
            self.assertEqual(processes.task_processes("/wt/demo-t1"), {})
        proc.is_running.return_value = True
        with mock.patch.object(processes.psutil, "process_iter", return_value=[proc]):
            with self.assertRaisesRegex(RuntimeError, "cannot inspect process"):
                processes.task_processes("/wt/demo-t1")


class CodexStopTest(unittest.TestCase):
    """Real processes through Runtime.stop_task_worker; nothing is signalled that this test did not start."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.wt = self.root / "wt-task"
        self.wt.mkdir()
        self.procs = []
        patcher = mock.patch.dict(os.environ, {"ORCHD_HOME": str(self.root / "home")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.rt = Runtime()
        self.rt.sleep = lambda s: time.sleep(min(s, 0.05))
        self.marks = (str(self.wt), "thread-1")

    def tearDown(self):
        for proc in self.procs:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    pass
                proc.wait()
        self.tmp.cleanup()

    def start(self, argv, env=None):
        proc = subprocess.Popen(argv, start_new_session=True, env=env or os.environ.copy(),
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.procs.append(proc)
        time.sleep(0.2)
        return proc

    def look_alike(self, env=None):
        """Unrelated process whose command line holds `codex` and this task's worktree and thread."""
        return self.start([sys.executable, "-c", "import time; time.sleep(120)", "codex", str(self.wt), "thread-1"], env)

    def test_unrelated_process_with_codex_and_the_path_in_its_command_is_not_signalled(self):
        other = self.look_alike()
        self.rt.stop_task_worker("codex", str(other.pid), self.marks)
        self.assertIsNone(other.poll(), "an unrelated process was killed because its command held codex and the path")

    def test_stop_codex_leaves_the_look_alike_alone_too(self):
        other = self.look_alike()
        self.rt.stop_codex(str(other.pid))
        self.assertIsNone(other.poll())

    def test_reused_pid_with_a_different_birth_is_not_signalled(self):
        other = self.look_alike()
        directory = self.root / "home" / "codex-pids"
        directory.mkdir(parents=True)
        proc = psutil.Process(other.pid)
        (directory / f"{other.pid}.json").write_text(json.dumps(
            dict(pid=other.pid, birth=proc.create_time() - 500, pgid=os.getpgid(other.pid))))
        self.rt.stop_task_worker("codex", str(other.pid), self.marks)
        self.assertIsNone(other.poll())

    def test_process_marked_for_another_task_is_not_signalled(self):
        other = self.look_alike(dict(os.environ, ORCHD_WORKTREE="/some/other/worktree"))
        self.rt.stop_task_worker("codex", str(other.pid), self.marks)
        self.assertIsNone(other.poll())

    def test_unreadable_look_alike_blocks_instead_of_guessing(self):
        other = self.look_alike()
        real = psutil.Process(other.pid)

        class Denied:  # the real process, but its environment cannot be read
            def __getattr__(self, name):
                return getattr(real, name)

            def environ(self):
                raise psutil.AccessDenied(other.pid)
        denied = Denied()
        shim = types.SimpleNamespace(**{**vars(psutil), "Process": lambda pid: denied})
        with mock.patch("orchd.runtime.psutil", shim):
            with self.assertRaisesRegex(RuntimeError, "not confirmed stopped"):
                self.rt.stop_task_worker("codex", str(other.pid), self.marks)
        self.assertIsNone(other.poll())

    def test_recorded_turn_is_stopped_but_a_foreign_marked_descendant_survives(self):
        pidfile = self.root / "child.pid"
        script = self.root / "turn.sh"
        script.write_text(f'#!/bin/sh\nORCHD_WORKTREE=/some/other/worktree {sys.executable} -c "import time; time.sleep(120)" &\necho $! > "{pidfile}"\nsleep 120\n')
        script.chmod(0o755)
        pid = self.rt.spawn([str(script)], str(self.wt), str(self.root / "log"))
        self.addCleanup(lambda: os.killpg(int(pid), signal.SIGKILL) if self.rt.pid_alive(pid) else None)
        for _ in range(100):
            if pidfile.exists() and pidfile.read_text().strip():
                break
            time.sleep(0.05)
        foreign = int(pidfile.read_text())
        self.addCleanup(lambda: os.kill(foreign, signal.SIGKILL) if psutil.pid_exists(foreign) else None)
        self.rt.stop_task_worker("codex", str(pid), self.marks, wait=3)
        self.assertFalse(self.rt.pid_alive(pid))
        self.assertTrue(psutil.pid_exists(foreign), "a member of the group marked for another task was signalled")

    def test_recorded_turn_is_stopped_without_any_command_string_match(self):
        script = self.root / "quiet.sh"
        script.write_text("#!/bin/sh\nsleep 120\n")
        script.chmod(0o755)
        pid = self.rt.spawn([str(script)], str(self.wt), str(self.root / "log"))
        self.addCleanup(lambda: os.killpg(int(pid), signal.SIGKILL) if self.rt.pid_alive(pid) else None)
        time.sleep(0.2)
        self.rt.stop_task_worker("codex", str(pid), self.marks, wait=3)
        self.assertFalse(self.rt.pid_alive(pid))


class CodexIdentityOwnershipTest(unittest.TestCase):
    def test_another_tasks_record_overwriting_a_reused_pid_cannot_authorize_close(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, ORCHD_HOME=home):
            rt = Runtime()
            proc = mock.Mock(spec=psutil.Process, pid=4242)
            proc.status.return_value = psutil.STATUS_RUNNING
            proc.create_time.return_value = 100
            proc.environ.return_value = {"ORCHD_WORKTREE": "/wt/A"}
            with mock.patch("orchd.runtime.psutil.Process", return_value=proc), \
                    mock.patch("orchd.runtime.os.getpgid", return_value=4242), \
                    mock.patch.object(rt, "pid_alive", return_value=True):
                rt._record_identity(4242)
                proc.create_time.return_value = 200
                proc.environ.return_value = {"ORCHD_WORKTREE": "/wt/B"}
                rt._record_identity(4242)
                rt.stop_codex(4242, ("/wt/A",))
                proc.terminate.assert_not_called()
                proc.kill.assert_not_called()
                self.assertEqual(rt._codex_targets(4242, ("/wt/B",)), [proc])

    def test_identity_expiry_keeps_live_unknown_and_non_pid_files(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, ORCHD_HOME=home):
            rt = Runtime()
            directory = Path(home, "codex-pids")
            directory.mkdir()
            names = ("10.json", "11.json", "12.json", "notes.json", "١٣.json")
            for name in names:
                pid = int(name[:-5]) if name[:-5].isdecimal() else 99
                path = directory / name
                path.write_text(json.dumps(dict(pid=pid, birth=100, pgid=pid, worktree="/wt/A")))
                os.utime(path, (1, 1))
            (directory / "14.json").mkdir()
            (directory / "15.json").symlink_to(directory / "notes.json")
            current = mock.Mock(spec=psutil.Process, pid=4242)
            current.create_time.return_value = 200
            live = mock.Mock(spec=psutil.Process, pid=10)
            live.create_time.return_value = 100
            def process(pid):
                if pid == 4242:
                    return current
                if pid == 10:
                    return live
                if pid == 11:
                    raise psutil.AccessDenied(pid)
                raise psutil.NoSuchProcess(pid)
            with mock.patch("orchd.runtime.psutil.Process", side_effect=process), \
                    mock.patch("orchd.runtime.os.getpgid", return_value=4242):
                rt._record_identity(4242, worktree="/wt/B")
            self.assertFalse((directory / "12.json").exists())
            for name in ("10.json", "11.json", "notes.json", "١٣.json", "14.json", "15.json", "4242.json"):
                self.assertTrue((directory / name).exists(), name)

    def test_legacy_unowned_record_requires_a_matching_environment(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, ORCHD_HOME=home):
            rt = Runtime()
            path = rt._identity_file(4242)
            path.parent.mkdir()
            path.write_text(json.dumps(dict(pid=4242, birth=100, pgid=4242)))
            proc = mock.Mock(spec=psutil.Process, pid=4242)
            proc.status.return_value = psutil.STATUS_RUNNING
            proc.create_time.return_value = 100
            proc.environ.return_value = {"ORCHD_WORKTREE": "/wt/B"}
            with mock.patch("orchd.runtime.psutil.Process", return_value=proc), \
                    mock.patch("orchd.runtime.os.getpgid", return_value=4242):
                rt.stop_codex(4242, ("/wt/A",))
                proc.terminate.assert_not_called()
                proc.environ.return_value = {"ORCHD_WORKTREE": "/wt/A"}
                self.assertEqual(rt._codex_targets(4242, ("/wt/A",)), [proc])


class StuckCloseReadOnlyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("ORCHD_")}
        self.env.update(ORCHD_HOME=str(self.home), ORCHD_AUTO_MIGRATE="0", HOME=self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self):
        return subprocess.run([sys.executable, str(ROOT / "bin" / "orchd"), "stuck-close"], env=self.env,
                              capture_output=True, text=True, timeout=60)

    def snapshot(self):
        return sorted((str(p.relative_to(self.tmp.name)), p.stat().st_mtime_ns, p.stat().st_size)
                      for p in Path(self.tmp.name).rglob("*"))

    def test_absent_database_is_reported_and_nothing_is_created(self):
        before = self.snapshot()
        done = self.run_cli()
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("no database", done.stderr)
        self.assertFalse(self.home.exists())
        self.assertEqual(self.snapshot(), before)

    def test_old_database_is_reported_and_not_migrated(self):
        self.home.mkdir()
        db = self.home / "orchd.db"
        with sqlite3.connect(db) as con:
            con.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT)")
            con.execute("INSERT INTO tasks VALUES ('old1', 'running')")
        con.close()
        before_bytes, before = db.read_bytes(), self.snapshot()
        done = self.run_cli()
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("older", done.stderr)
        self.assertEqual(db.read_bytes(), before_bytes)
        self.assertEqual(self.snapshot(), before)
        with sqlite3.connect(db) as check:
            self.assertEqual([r[1] for r in check.execute("PRAGMA table_info(tasks)")], ["id", "status"])
            self.assertEqual(check.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(), [("tasks",)])
        check.close()

    def test_listing_preserves_database_wal_and_shm_hashes(self):
        self.home.mkdir()
        db = self.home / "orchd.db"
        con = store.connect(db)
        self.addCleanup(con.close)
        con.execute("PRAGMA wal_autocheckpoint=0")
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        store.create_task(con, id="abcd1234", repo="demo", repo_path="/r", title="T", instructions="i", done_when="d",
                          orch_thread="th", codex_bin="c", model="claude-sonnet-5-5", worktree="/wt", status="done")
        store.update_task(con, "abcd1234", note="close pending, worker not confirmed stopped")
        # Keep a writer alive, and copy its committed DB/WAL into isolated fixtures. Never unlink a live SHM.
        for state in ("wal_without_shm", "wal_with_shm", "no_wal"):
            with self.subTest(state=state):
                directory = Path(self.tmp.name) / state
                directory.mkdir()
                if state == "no_wal":
                    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                for suffix in (("",) if state == "no_wal" else ("", "-wal", "-shm")):
                    if state == "wal_without_shm" and suffix == "-shm":
                        continue
                    (directory / ("orchd.db" + suffix)).write_bytes(Path(str(db) + suffix).read_bytes())
                def hashes():
                    return {p.name: (p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
                            for p in directory.iterdir()}
                before = hashes()
                with mock.patch.dict(self.env, ORCHD_HOME=str(directory)):
                    done = self.run_cli()
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertEqual([row["task_id"] for row in json.loads(done.stdout)], ["abcd1234"])
                self.assertEqual(hashes(), before)

    def test_current_database_lists_pending_closes_with_no_file_changes(self):
        self.home.mkdir()
        con = store.connect(self.home / "orchd.db")
        store.create_task(con, id="abcd1234", repo="demo", repo_path="/r", title="T", instructions="i", done_when="d",
                          orch_thread="th", codex_bin="c", model="claude-sonnet-5-5", worktree="/wt", status="done")
        store.update_task(con, "abcd1234", note="close pending, worker not confirmed stopped")
        con.close()
        before = self.snapshot()
        done = self.run_cli()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual([row["task_id"] for row in json.loads(done.stdout)], ["abcd1234"])
        self.assertEqual(self.snapshot(), before)


class RunbookTest(unittest.TestCase):
    def test_manual_cleanup_is_a_full_checklist_with_backups_before_force(self):
        text = (ROOT / "docs" / "runbooks" / "stuck-close.md").read_text()
        force = text.index("$REPO worktree remove --force")
        before_force = text[:force]
        for needle in ("--ignored", "branch -vv", "tag --contains", "reflog", "stash list", "rev-list", "--not --remotes",
                       "detached", "no upstream", "absolute-git-dir", "cp -R"):
            self.assertIn(needle, before_force, f"runbook lacks a check or backup for: {needle}")


if __name__ == "__main__":
    unittest.main()
