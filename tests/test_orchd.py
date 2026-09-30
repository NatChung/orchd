import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from orchd import core, mcp_server, store
from orchd.runtime import Runtime


class FakeRuntime:
    codex = "/fake/codex"

    def __init__(self):
        self.sent, self.woken, self.stopped, self.removed, self.viewed = [], [], [], [], []
        self.wake_fails = False
        self.removable = (True, "pushed")
        self.jobs = {"job1": {}}

    def repo_path(self, repo):
        if repo == "missing":
            raise ValueError("not a repo")
        return Path("/projects") / repo

    def claude_trusted(self, repo_path):
        return repo_path.name != "untrusted"

    def create_worktree(self, repo_path, repo, task_id):
        return "base123", f"orchd/{task_id}", f"/projects/.orchd-worktrees/{repo}-{task_id}"

    def socket_path(self, task_id):
        return f"/tmp/orchd-{task_id}/w.sock"

    def start_worker(self, worktree, sock, brief):
        self.brief = brief
        return "job1", "session1"

    def send_uds(self, path, session_id, text):
        self.sent.append((path, session_id, text))

    def wake_orch(self, codex_bin, thread, text):
        if self.wake_fails:
            raise OSError("queue down")
        self.woken.append((codex_bin, thread, text))

    def live_jobs(self):
        return self.jobs

    def stop_worker(self, job):
        self.stopped.append(job)

    def worktree_state(self, worktree, base):
        return self.removable

    def remove_worktree(self, repo_path, worktree):
        self.removed.append(worktree)

    def open_viewer(self, job):
        self.viewed.append(job)


class CoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = store.connect(Path(self.tmp.name) / "t.db")
        self.rt = FakeRuntime()

    def tearDown(self):
        self.tmp.cleanup()

    def dispatch(self, thread="thread-A"):
        return core.dispatch(self.con, self.rt, orch_thread=thread, repo="demo", title="T",
                             instructions="do it", done_when="tests pass")

    def test_dispatch_records_caller_thread_and_sends_task(self):
        t = self.dispatch()
        self.assertEqual((t["status"], t["orch_thread"], t["job_id"]), ("running", "thread-A", "job1"))
        path, session, text = self.rt.sent[0]
        self.assertEqual(session, "session1")
        self.assertIn(f"[orchd task {t['id']}]", text)
        self.assertIn("tests pass", text)
        self.assertIn("Never push directly to the default branch", self.rt.brief)
        self.assertIn("Never review-and-merge a PR you authored", self.rt.brief)

    def test_dispatch_without_thread_is_refused(self):
        with self.assertRaises(ValueError):
            core.dispatch(self.con, self.rt, orch_thread=None, repo="demo", title="T",
                          instructions="x", done_when="y")

    def test_failed_launch_marks_task_failed(self):
        self.rt.start_worker = lambda *a: (_ for _ in ()).throw(RuntimeError("no socket"))
        with self.assertRaises(RuntimeError):
            self.dispatch()
        (row,) = store.open_tasks(self.con)
        self.assertEqual(row["status"], "failed")
        self.assertIn("no socket", row["note"])
        self.assertEqual(self.rt.removed, [row["worktree"]])

    def test_untrusted_repo_is_refused_before_any_worktree(self):
        with self.assertRaisesRegex(ValueError, "trust"):
            core.dispatch(self.con, self.rt, orch_thread="t", repo="untrusted", title="T",
                          instructions="x", done_when="y")
        self.assertEqual(store.open_tasks(self.con), [])

    def test_report_wakes_the_dispatching_orch_only(self):
        a = self.dispatch("thread-A")
        b = self.dispatch("thread-B")
        core.ack(self.con, a["id"])
        self.assertTrue(core.report(self.con, self.rt, a["id"], "done", "added line", "commit abc"))
        self.assertEqual(self.rt.woken[-1][1], "thread-A")
        inbox_a = core.inbox(self.con, "thread-A")
        self.assertEqual([m["kind"] for m in inbox_a], ["ack", "report"])
        self.assertEqual(inbox_a[1]["evidence"], "commit abc")
        self.assertEqual(core.inbox(self.con, "thread-A"), [])
        self.assertEqual(core.inbox(self.con, "thread-B"), [])
        self.assertEqual(store.get_task(self.con, b["id"])["status"], "running")

    def test_report_is_kept_when_wake_fails(self):
        t = self.dispatch()
        self.rt.wake_fails = True
        self.assertFalse(core.report(self.con, self.rt, t["id"], "blocked", "need token", ""))
        (msg,) = core.inbox(self.con, "thread-A")
        self.assertEqual(msg["body"], "blocked: need token")
        self.assertIn("queue down", self.con.execute("SELECT wake_error FROM messages").fetchone()[0])

    def test_question_and_answer_round_trip(self):
        t = self.dispatch()
        core.ask(self.con, self.rt, t["id"], "Send this email?\n---\nHi Chris")
        self.assertEqual(store.get_task(self.con, t["id"])["status"], "question")
        (q,) = core.inbox(self.con, "thread-A")
        self.assertIn("Hi Chris", q["body"])
        core.answer(self.con, self.rt, t["id"], "Nat: yes, send it")
        self.assertEqual(self.rt.sent[-1][2], f"[orchd answer {t['id']}]\nNat: yes, send it")

    def test_list_open_spans_all_threads_and_shows_liveness(self):
        self.dispatch("thread-A")
        self.dispatch("thread-B")
        self.rt.jobs = {}
        rows = core.list_open(self.con, self.rt)
        self.assertEqual({r["orch_thread"] for r in rows}, {"thread-A", "thread-B"})
        self.assertEqual({r["worker_alive"] for r in rows}, {False})

    def test_close_keeps_unsafe_worktree(self):
        t = self.dispatch()
        self.rt.removable = (False, "uncommitted changes")
        result = core.close(self.con, self.rt, t["id"])
        self.assertIn("kept", result["worktree"])
        self.assertEqual(self.rt.removed, [])
        self.assertEqual(self.rt.stopped, ["job1"])
        self.assertEqual(store.open_tasks(self.con), [])

    def test_close_removes_safe_worktree(self):
        t = self.dispatch()
        self.assertEqual(core.close(self.con, self.rt, t["id"])["worktree"], "removed")
        self.assertEqual(self.rt.removed, [t["worktree"]])


class McpTest(unittest.TestCase):
    def run_server(self, *messages):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        con = store.connect(Path(tmp.name) / "t.db")
        out = io.StringIO()
        mcp_server.serve(io.StringIO("".join(json.dumps(m) + "\n" for m in messages)), out, con, FakeRuntime())
        return [json.loads(line) for line in out.getvalue().splitlines()]

    def test_handshake_list_and_dispatch_uses_meta_thread(self):
        replies = self.run_server(
            {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                "name": "dispatch", "_meta": {"threadId": "thread-X"},
                "arguments": {"repo": "demo", "title": "T", "instructions": "i", "done_when": "d"}}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "list_open", "arguments": {}}},
        )
        self.assertEqual([r["id"] for r in replies], [0, 1, 2, 3])
        self.assertEqual({t["name"] for t in replies[1]["result"]["tools"]},
                         {"dispatch", "inbox", "list_open", "answer", "close", "view_worker"})
        self.assertNotIn("isError", replies[2]["result"])
        (row,) = json.loads(replies[3]["result"]["content"][0]["text"])
        self.assertEqual(row["orch_thread"], "thread-X")

    def test_tool_errors_are_reported_not_raised(self):
        (reply,) = self.run_server({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {
            "name": "dispatch", "_meta": {"threadId": "t"},
            "arguments": {"repo": "missing", "title": "T", "instructions": "i", "done_when": "d"}}})
        self.assertTrue(reply["result"]["isError"])


class WorktreeStateTest(unittest.TestCase):
    """Real git: removal is allowed only when nothing local would be lost."""

    def git(self, *args, cwd):
        return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.remote = root / "remote.git"
        self.git("init", "-q", "--bare", "-b", "main", str(self.remote), cwd=root)
        self.projects = root / "projects"
        repo = self.projects / "demo"
        repo.mkdir(parents=True)
        self.git("init", "-q", "-b", "main", cwd=repo)
        self.git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init", cwd=repo)
        self.git("remote", "add", "origin", str(self.remote), cwd=repo)
        self.git("push", "-q", "-u", "origin", "main", cwd=repo)
        self.git("remote", "set-head", "origin", "main", cwd=repo)
        os.environ["ORCHD_PROJECTS"] = str(self.projects)
        self.rt = Runtime()
        self.repo = self.rt.repo_path("demo")
        self.base, self.branch, self.wt = self.rt.create_worktree(self.repo, "demo", "abcd1234")

    def tearDown(self):
        os.environ.pop("ORCHD_PROJECTS", None)
        self.tmp.cleanup()

    def commit(self):
        Path(self.wt, "f.txt").write_text("x")
        self.git("add", "f.txt", cwd=self.wt)
        self.git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "c", cwd=self.wt)

    def test_fresh_worktree_is_removable(self):
        self.assertEqual(self.rt.worktree_state(self.wt, self.base), (True, "no new commits"))
        self.rt.remove_worktree(self.repo, self.wt)
        self.assertFalse(Path(self.wt).exists())

    def test_dirty_and_unpushed_are_kept(self):
        Path(self.wt, "f.txt").write_text("x")
        self.assertEqual(self.rt.worktree_state(self.wt, self.base)[0], False)
        self.git("add", "f.txt", cwd=self.wt)
        self.git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "c", cwd=self.wt)
        self.assertEqual(self.rt.worktree_state(self.wt, self.base), (False, "commits not pushed (no upstream)"))

    def test_pushed_branch_is_removable(self):
        self.commit()
        self.git("push", "-q", "-u", "origin", self.branch, cwd=self.wt)
        self.assertEqual(self.rt.worktree_state(self.wt, self.base), (True, "pushed"))

    def test_repo_must_be_direct_child_of_projects(self):
        with self.assertRaises(ValueError):
            self.rt.repo_path("../etc")


if __name__ == "__main__":
    unittest.main()
