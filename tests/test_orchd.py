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
        self.resumed, self.alive_pids = [], set()

    def repo_path(self, repo):
        if repo == "missing":
            raise ValueError("not a repo")
        return Path("/projects") / repo

    def claude_trusted(self, repo_path):
        return Path(repo_path).name != "untrusted"

    def create_worktree(self, repo_path, repo, task_id):
        return "base123", f"orchd/{task_id}", f"/projects/.orchd-worktrees/{repo}-{task_id}"

    def socket_path(self, task_id):
        return f"/tmp/orchd-{task_id}/w.sock"

    def start_worker(self, worktree, sock, brief, model):
        self.brief, self.model = brief, model
        return "job1", "session1"

    def orch_socket_path(self, orch_id):
        return f"/tmp/orchd-o-{orch_id}/o.sock"

    def start_orch(self, orch_id, model, orch_home):
        self.orch_started = (orch_id, model, str(orch_home))
        return self.orch_socket_path(orch_id), "orchjob", "orchsession"

    def attach(self, job):
        self.attached = job

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

    def remove_worktree(self, repo_path, worktree, base=None):
        self.removed.append(worktree)

    def open_viewer(self, job):
        self.viewed.append(job)

    def codex_log(self, task_id):
        return f"/tmp/orchd-{task_id}/codex.jsonl"

    def start_codex_worker(self, worktree, log, prompt, model):
        self.codex_prompt, self.model = prompt, model
        return "4242", "thread-W"

    def resume_codex_worker(self, worktree, log, thread, text, model):
        self.resumed.append((thread, text, model))
        return "4343"

    def pid_alive(self, pid):
        return pid in self.alive_pids

    def stop_codex(self, pid):
        self.stopped.append(("codex", pid))

    def stop_task_worker(self, kind, job, marks=()):
        self.stopped.append(job if kind == "claude" else (kind, job))

    def open_codex_viewer(self, worktree, thread):
        self.viewed.append(("codex", thread))

    claude_usage = Runtime.claude_usage
    codex_usage = Runtime.codex_usage


class CoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = store.connect(Path(self.tmp.name) / "t.db")
        self.rt = FakeRuntime()

    def tearDown(self):
        self.tmp.cleanup()

    def dispatch(self, thread="thread-A", repo="demo", **kw):
        kw = {"model": "sonnet", "model_reason": "clear scope", "task_type": "code", **kw}
        return core.dispatch(self.con, self.rt, orch_thread=thread, repo=repo, title="T",
                             instructions="do it", done_when="tests pass", **kw)

    def test_dispatch_records_caller_thread_and_sends_task(self):
        t = self.dispatch()
        self.assertEqual((t["status"], t["orch_thread"], t["job_id"]), ("running", "thread-A", "job1"))
        path, session, text = self.rt.sent[0]
        self.assertEqual(session, "session1")
        self.assertIn(f"[orchd task {t['id']}]", text)
        self.assertIn("tests pass", text)
        self.assertIn("Never push directly to the default branch", self.rt.brief)
        self.assertIn("Never review-and-merge a PR you authored", self.rt.brief)

    def test_non_default_home_is_carried_to_the_worker(self):
        os.environ["ORCHD_HOME"] = "/tmp/iso home"
        try:
            t = self.dispatch()
        finally:
            os.environ.pop("ORCHD_HOME")
        self.assertIn("ORCHD_HOME='/tmp/iso home' ", self.rt.brief)
        self.assertIn(f"ORCHD_HOME='/tmp/iso home' {core.ORCHD} ack {t['id']}", self.rt.sent[0][2])

    def test_dispatch_without_thread_is_refused(self):
        with self.assertRaises(ValueError):
            self.dispatch(thread=None)

    def test_failed_launch_marks_task_failed(self):
        self.rt.start_worker = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no socket"))
        with self.assertRaises(RuntimeError):
            self.dispatch()
        (row,) = store.open_tasks(self.con)
        self.assertEqual(row["status"], "failed")
        self.assertIn("no socket", row["note"])
        self.assertEqual(self.rt.removed, [row["worktree"]])

    def test_untrusted_repo_is_refused_before_any_worktree(self):
        with self.assertRaisesRegex(ValueError, "trust"):
            self.dispatch("t", repo="untrusted")
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

    def test_notifications_name_the_stored_model_and_keep_task_and_kind(self):
        for model in core.WORKER_MODELS.values():
            t = self.dispatch()
            store.update_task(self.con, t["id"], model=model)  # the stored model is what the text must show
            before = len(self.rt.woken)
            core.ack(self.con, t["id"])
            core.progress(self.con, self.rt, t["id"], "half way")
            core.ask(self.con, self.rt, t["id"], "ok?\nmore")
            core.report(self.con, self.rt, t["id"], "done", "finished", "")
            texts = [w[2] for w in self.rt.woken[before:]]
            self.assertEqual(len(texts), 3)
            for text, kind in zip(texts, ("progress", "question", "done")):
                self.assertIn(f"[orchd] demo/{t['id']} ({model}) {kind}: ", text)

    def test_notification_without_stored_model_says_unknown(self):
        for model in (None, ""):
            text = core.wake_text(dict(repo="r", id="abc", model=model), "done: x")
            self.assertIn("r/abc (unknown) done: x", text)

    def test_report_is_kept_when_wake_fails(self):
        t = self.dispatch()
        self.rt.wake_fails = True
        self.assertFalse(core.report(self.con, self.rt, t["id"], "blocked", "need token", ""))
        (msg,) = core.inbox(self.con, "thread-A")
        self.assertEqual(msg["body"], "blocked: need token")
        self.assertIn("queue down", self.con.execute("SELECT wake_error FROM messages WHERE kind='report'").fetchone()[0])

    def test_question_and_answer_round_trip(self):
        t = self.dispatch()
        core.ask(self.con, self.rt, t["id"], "Send this email?\n---\nHi Chris")
        self.assertEqual(store.get_task(self.con, t["id"])["status"], "question")
        (q,) = core.inbox(self.con, "thread-A")
        self.assertIn("Hi Chris", q["body"])
        core.answer(self.con, self.rt, t["id"], "Nat: yes, send it")
        self.assertEqual(self.rt.sent[-1][2], f"[orchd answer {t['id']}]\nNat: yes, send it")

    def test_progress_reaches_orch_inbox_and_keeps_task_running(self):
        t = self.dispatch()
        core.ack(self.con, t["id"])
        self.assertTrue(core.progress(self.con, self.rt, t["id"], "found the channel\nmore detail"))
        self.assertEqual(store.get_task(self.con, t["id"])["status"], "acked")
        self.assertIn("progress: found the channel", self.rt.woken[-1][2])
        self.assertEqual([m["kind"] for m in core.inbox(self.con, "thread-A")], ["ack", "progress"])

    def test_brief_routes_progress_through_orchd_and_forbids_peer_messaging(self):
        self.dispatch()
        self.assertIn("progress <task-id>", self.rt.brief)
        self.assertIn("Never use SendMessage", self.rt.brief)
        self.assertIn("in the background", self.rt.brief)

    def test_claude_orch_is_woken_over_its_socket(self):
        store.register_orch(self.con, "o-claude", "claude", model="claude-opus-5-5",
                            socket="/tmp/orchd-o-x/o.sock", session_id="orch-session")
        t = self.dispatch("o-claude")
        core.report(self.con, self.rt, t["id"], "done", "ok", "")
        self.assertEqual(self.rt.woken, [])
        path, session, text = self.rt.sent[-1]
        self.assertEqual((path, session), ("/tmp/orchd-o-x/o.sock", "orch-session"))
        self.assertIn("done: ok", text)

    def test_unregistered_thread_falls_back_to_codex_queue(self):
        t = self.dispatch("legacy-codex-thread")
        core.report(self.con, self.rt, t["id"], "done", "ok", "")
        self.assertEqual(self.rt.woken[-1][:2], ("/fake/codex", "legacy-codex-thread"))

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

    def test_dispatch_defaults_to_sol_and_logs_event(self):
        t = core.dispatch(self.con, self.rt, orch_thread="thread-A", repo="demo", title="T",
                          instructions="do it", done_when="tests pass", model_reason="clear scope", task_type="code")
        self.assertEqual((t["model"], t["model_reason"], t["task_type"]), ("gpt-6.1-sol", "clear scope", "code"))
        self.assertEqual(self.rt.model, "gpt-6.1-sol")
        row = self.con.execute("SELECT body FROM messages WHERE kind='dispatch'").fetchone()
        self.assertEqual(json.loads(row[0])["model_reason"], "clear scope")
        self.assertEqual(self.dispatch(model="sonnet")["model"], "claude-sonnet-5-5")

    def test_dispatch_refuses_removed_opus_before_creating_a_task(self):
        for model in ("opus", "claude-opus-5-5"):
            with self.subTest(model=model), self.assertRaisesRegex(ValueError, "use one of sol, sonnet"):
                self.dispatch(model=model)
        self.assertEqual(store.open_tasks(self.con), [])
        self.assertEqual((self.rt.sent, self.rt.removed), ([], []))

    def test_dispatch_refuses_bad_parameters_before_any_worktree(self):
        bad = [dict(model="gpt"), dict(model_reason=""), dict(model_reason=None), dict(task_type="nope"),
               dict(task_type=None), dict(rework_of="deadbeef"), dict(found_by="nat")]
        for kw in bad:
            with self.assertRaises(ValueError, msg=str(kw)):
                self.dispatch(**kw)
        with self.assertRaisesRegex(ValueError, "found_by"):
            self.dispatch(rework_of=self.dispatch()["id"], found_by="bogus")
        self.assertEqual(len(store.open_tasks(self.con)), 1)

    def test_rework_of_and_found_by_are_stored(self):
        first = self.dispatch()
        t = self.dispatch(rework_of=first["id"], found_by="verify")
        self.assertEqual((t["rework_of"], t["found_by"]), (first["id"], "verify"))

    def test_other_open_on_repo_lists_only_other_orchs_open_tasks(self):
        mine = self.dispatch("thread-A")
        theirs = self.dispatch("thread-B")
        self.dispatch("thread-C", repo="other")
        closed = self.dispatch("thread-D")
        core.close(self.con, self.rt, closed["id"])
        rows = core.other_open_on_repo(self.con, "demo", "thread-A", mine["id"])
        self.assertEqual([r["task_id"] for r in rows], [theirs["id"]])
        self.assertEqual(rows[0]["orch_thread"], "thread-B")
        self.assertEqual(core.other_open_on_repo(self.con, "nowhere", "thread-A"), [])

    def test_close_stores_outcome_rating_and_deduplicated_usage(self):
        t = self.dispatch()
        proj = Path(self.tmp.name) / "projects" / "-x"
        proj.mkdir(parents=True)
        line = lambda mid, out: json.dumps({"type": "assistant", "message": {"id": mid, "usage": {
            "input_tokens": 3, "output_tokens": out, "cache_creation_input_tokens": 10, "cache_read_input_tokens": 100}}})
        (proj / "session1.jsonl").write_text("\n".join([line("m1", 5), line("m1", 7), line("m2", 2),
                                                        json.dumps({"type": "user"}), "not json"]))
        os.environ["ORCHD_CLAUDE_PROJECTS"] = str(proj.parent)
        self.addCleanup(os.environ.pop, "ORCHD_CLAUDE_PROJECTS", None)
        core.close(self.con, self.rt, t["id"], outcome="merged", rating=3)
        row = store.get_task(self.con, t["id"])
        self.assertEqual((row["outcome"], row["rating"], row["status"]), ("merged", 3, "closed"))
        usage = json.loads(self.con.execute("SELECT body FROM messages WHERE kind='usage'").fetchone()[0])
        self.assertEqual(usage, dict(model="claude-sonnet-5-5", input_tokens=6, output_tokens=9,
                                     cache_creation_input_tokens=20, cache_read_input_tokens=200, messages=2))
        self.assertEqual(json.loads(self.con.execute("SELECT body FROM messages WHERE kind='close'").fetchone()[0]),
                         {"outcome": "merged", "rating": 3})

    def test_close_without_transcript_or_bad_fields(self):
        t = self.dispatch()
        with self.assertRaises(ValueError):
            core.close(self.con, self.rt, t["id"], outcome="bogus")
        with self.assertRaises(ValueError):
            core.close(self.con, self.rt, t["id"], rating=4)
        self.assertEqual(store.get_task(self.con, t["id"])["status"], "running")
        os.environ["ORCHD_CLAUDE_PROJECTS"] = self.tmp.name
        self.addCleanup(os.environ.pop, "ORCHD_CLAUDE_PROJECTS", None)
        self.assertTrue(core.close(self.con, self.rt, t["id"])["closed"])
        self.assertIsNone(self.con.execute("SELECT 1 FROM messages WHERE kind='usage'").fetchone())

    def test_close_removes_safe_worktree(self):
        t = self.dispatch()
        self.assertEqual(core.close(self.con, self.rt, t["id"])["worktree"], "removed")
        self.assertEqual(self.rt.removed, [t["worktree"]])


class CodexWorkerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = store.connect(Path(self.tmp.name) / "t.db")
        self.rt = FakeRuntime()

    def tearDown(self):
        self.tmp.cleanup()

    def dispatch(self, repo="demo"):
        return core.dispatch(self.con, self.rt, orch_thread="thread-A", repo=repo, title="T", instructions="do it",
                             done_when="tests pass", model="sol", model_reason="second vendor", task_type="code")

    def test_sol_runs_on_codex_with_brief_and_task_in_one_prompt(self):
        t = self.dispatch(repo="untrusted")  # Claude's trust list does not gate a Codex worker
        self.assertEqual((t["model"], t["job_id"], t["session_id"], t["socket"]), ("gpt-6.1-sol", "4242", "thread-W", None))
        self.assertEqual(self.rt.sent, [])
        self.assertIn("Codex session", self.rt.codex_prompt)
        self.assertIn("end your turn right away", self.rt.codex_prompt)
        self.assertIn(f"[orchd task {t['id']}]", self.rt.codex_prompt)

    def test_answer_resumes_the_thread_only_between_turns(self):
        t = self.dispatch()
        core.ask(self.con, self.rt, t["id"], "Send it?")
        self.rt.alive_pids = {"4242"}
        self.rt.sleep = lambda s: None
        self.assertEqual(core.answer(self.con, self.rt, t["id"], "yes"), dict(status="queued", delivered=0, pending=1))
        self.assertEqual(self.rt.resumed, [])
        polls = []
        self.rt.sleep = lambda s: polls.append(s) or (len(polls) == 3 and self.rt.alive_pids.clear())
        # the asking turn exits a moment after the Orch is woken
        self.assertEqual(core.answer(self.con, self.rt, t["id"], flush=True), dict(status="delivered", delivered=1, pending=0))
        self.assertEqual(len(polls), 3)
        self.assertEqual(self.rt.resumed, [("thread-W", f"[orchd answer {t['id']}]\nyes", "gpt-6.1-sol")])
        core.close(self.con, self.rt, t["id"])
        with self.assertRaisesRegex(ValueError, "closed"):
            core.answer(self.con, self.rt, t["id"], "late")
        self.assertEqual(store.get_task(self.con, t["id"])["job_id"], "4343")

    def test_liveness_counts_a_waiting_thread_as_unknown_and_a_silent_exit_as_dead(self):
        t = self.dispatch()
        alive = lambda: core.list_open(self.con, self.rt)[0]["worker_alive"]
        self.rt.alive_pids = {"4242"}
        self.assertTrue(alive())
        self.rt.alive_pids = set()
        self.assertFalse(alive())
        core.ask(self.con, self.rt, t["id"], "?")
        self.assertIsNone(alive())

    def test_close_kills_the_turn_and_reads_rollout_usage(self):
        t = self.dispatch()
        day = Path(self.tmp.name) / "sessions" / "2026" / "09" / "30"
        day.mkdir(parents=True)
        tok = lambda i, c, o: json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {
            "total_token_usage": {"input_tokens": i, "cached_input_tokens": c, "cache_write_input_tokens": 0,
                                  "output_tokens": o}}}})
        (day / "rollout-2026-09-30T19-47-32-thread-W.jsonl").write_text(
            "\n".join([tok(100, 40, 5), "not json", tok(300, 200, 9)]))
        os.environ["ORCHD_CODEX_SESSIONS"] = str(day.parents[2])
        self.addCleanup(os.environ.pop, "ORCHD_CODEX_SESSIONS", None)
        core.close(self.con, self.rt, t["id"])
        self.assertEqual(self.rt.stopped, [("codex", "4242")])
        usage = json.loads(self.con.execute("SELECT body FROM messages WHERE kind='usage'").fetchone()[0])
        self.assertEqual(usage, dict(model="gpt-6.1-sol", input_tokens=100, output_tokens=9,
                                     cache_creation_input_tokens=0, cache_read_input_tokens=200, messages=2))

    def test_view_refuses_mid_turn_then_opens_codex_resume(self):
        t = self.dispatch()
        self.rt.alive_pids = {"4242"}
        with self.assertRaisesRegex(ValueError, "in a turn"):
            core.view(self.con, self.rt, t["id"])
        self.rt.alive_pids = set()
        core.view(self.con, self.rt, t["id"])
        self.assertEqual(self.rt.viewed, [("codex", "thread-W")])

    def test_failed_codex_launch_marks_task_failed_and_drops_worktree(self):
        def boom(*a):
            raise RuntimeError("codex exec started no thread")
        self.rt.start_codex_worker = boom
        with self.assertRaises(RuntimeError):
            self.dispatch()
        (row,) = self.con.execute("SELECT status, note, worktree FROM tasks").fetchall()
        self.assertEqual(row["status"], "failed")
        self.assertIn("no thread", row["note"])
        self.assertEqual(self.rt.removed, [row["worktree"]])

    def test_finished_child_is_not_alive_without_another_subprocess(self):
        rt = Runtime()
        pid = subprocess.Popen(["/bin/sleep", "0.1"]).pid  # dropped Popen, as spawn does
        import time
        time.sleep(0.5)
        self.assertFalse(rt.pid_alive(pid))

    def test_resume_passes_the_task_model(self):
        rt = Runtime()
        rt.codex = "/fake/codex"
        seen = {}
        rt.spawn = lambda cmd, cwd, log: seen.update(cmd=cmd, cwd=cwd) or 7
        self.assertEqual(rt.resume_codex_worker("/wt", "/l", "th-1", "answer", "gpt-6.1-sol"), "7")
        self.assertEqual(seen["cmd"][seen["cmd"].index("-m") + 1], "gpt-6.1-sol")
        self.assertEqual(seen["cmd"][-2:], ["th-1", "answer"])
        self.assertEqual(seen["cwd"], "/wt")

    def test_stop_kills_only_a_live_codex_process(self):
        rt = Runtime()
        rt.run = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "/usr/bin/python3\n", "")
        killed = []
        orig, os.killpg = os.killpg, lambda pid, sig: killed.append(pid)
        try:
            rt.stop_codex("4242")  # a reused pid now running something else
            rt.run = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "/Users/x/.local/bin/codex\n", "")
            rt.stop_codex("4242")
        finally:
            os.killpg = orig
        self.assertEqual(killed, [4242])

    def test_start_parses_thread_id_from_exec_json(self):
        log = Path(self.tmp.name) / "codex.jsonl"
        rt = Runtime()
        rt.codex = "/fake/codex"
        seen = {}

        def spawn(cmd, cwd, path):
            seen["cmd"] = cmd
            Path(path).write_text('Reading additional input\n{"type":"thread.started","thread_id":"th-1"}\n')
            return 99
        rt.spawn, rt.pid_alive, rt.sleep = spawn, lambda pid: True, lambda s: None
        self.assertEqual(rt.start_codex_worker("/wt", str(log), "prompt", "gpt-6.1-sol"), ("99", "th-1"))
        self.assertEqual(seen["cmd"][:2], ["/fake/codex", "exec"])
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", seen["cmd"])
        self.assertEqual(seen["cmd"][-1], "prompt")


class OrchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = store.connect(Path(self.tmp.name) / "t.db")
        self.rt = FakeRuntime()

    def tearDown(self):
        os.environ.pop("ORCHD_ORCH_HOME", None)
        self.tmp.cleanup()

    def test_start_orch_registers_claude_row_with_socket_and_session(self):
        row = core.start_orch(self.con, self.rt)  # Orch default remains independent of worker default
        self.assertRegex(row["id"], r"^o[0-9a-f]{7}$")
        self.assertEqual((row["kind"], row["model"], row["session_id"], row["job_id"]),
                         ("claude", "claude-opus-5-5", "orchsession", "orchjob"))
        self.assertTrue(row["socket"].endswith("/o.sock"))
        core.stop_orch(self.con, self.rt, row["id"])
        self.assertEqual(self.rt.stopped, ["orchjob"])
        self.assertIsNotNone(store.get_orch(self.con, row["id"])["stopped_at"])

    def test_sonnet_orch_is_still_supported(self):
        row = core.start_orch(self.con, self.rt, "sonnet")
        self.assertEqual(row["model"], "claude-sonnet-5-5")

    def test_unknown_model_key_is_refused(self):
        with self.assertRaisesRegex(ValueError, "unknown model"):
            core.start_orch(self.con, self.rt, "gpt")
        with self.assertRaisesRegex(ValueError, "unknown model"):  # a Claude Orch cannot run a GPT model
            core.start_orch(self.con, self.rt, "sol")

    def test_untrusted_orch_home_is_refused(self):
        os.environ["ORCHD_ORCH_HOME"] = str(Path(self.tmp.name) / "untrusted")
        with self.assertRaisesRegex(ValueError, "trust"):
            core.start_orch(self.con, self.rt, "opus")
        self.assertFalse(hasattr(self.rt, "orch_started"))


class WatchTest(unittest.TestCase):
    def test_timeline_names_who_talks_to_whom(self):
        from orchd import watch
        base = dict(task_id="t1", evidence=None, created_at=0, repo="demo-shop-api", title="Fix tax",
                    model="claude-sonnet-5-5", model_reason="clear scope", orch_thread="o1234567")
        claude = dict(base, orch_kind="claude", orch_model="claude-opus-5-5")
        astra = dict(base, orch_kind=None, orch_model=None)
        line = lambda row, kind, body: watch.format_row(dict(row, kind=kind, body=body), color=False)
        self.assertIn("Claude Orch o1234567 → worker t1 (sonnet) [demo-shop-api] Fix tax · clear scope",
                      line(claude, "dispatch", '{"model_reason": "clear scope"}'))
        self.assertIn("worker t1 (sonnet) → Astra o1234567: asks: stack coupons?", line(astra, "question", "stack coupons?"))
        self.assertIn("Astra o1234567 → worker t1 (sonnet): no stacking", line(astra, "answer", "no stacking"))
        self.assertIn("closes worker t1 (sonnet): merged", line(claude, "close", '{"outcome": "merged"}'))

    def test_summary_measures_overlap(self):
        from orchd import watch
        with tempfile.TemporaryDirectory() as tmp:
            con = store.connect(Path(tmp) / "t.db")
            store.register_orch(con, "o1", "claude")
            for tid in ("a", "b"):
                store.create_task(con, id=tid, repo="r", repo_path="/r", title="T", instructions="i", done_when="d",
                                  orch_thread="o1", codex_bin="c", model="claude-sonnet-5-5")
                con.execute("UPDATE tasks SET created_at=100 WHERE id=?", (tid,))
                mid = store.add_message(con, tid, "report", "done: ok")
                con.execute("UPDATE messages SET created_at=160 WHERE id=?", (mid,))
            (row,) = watch.summary(con)
            self.assertEqual((row["workers"], row["worker_min"], row["wall_min"], row["parallel"], row["peak_workers"]),
                             (2, 2.0, 1.0, 2.0, 2))


class LaunchEnvTest(unittest.TestCase):
    def test_claude_launch_drops_parent_session_and_orch_identity(self):
        from orchd import runtime
        seen = {}
        rt = Runtime()
        rt.run = lambda cmd, **kw: seen.update(kw) or subprocess.CompletedProcess(cmd, 0, "claude attach j1", "")
        rt.agents = lambda: [{"id": "j1", "sessionId": "s1"}]
        rt.exists = lambda path: True
        keep = dict(os.environ)
        os.environ.update(CLAUDECODE="1", CLAUDE_CODE_SESSION_ID="parent", ORCHD_ORCH_ID="o1", ORCHD_HOME="/x")
        try:
            rt.start_worker("/wt", "/tmp/s", "brief", "claude-sonnet-5-5")
        finally:
            os.environ.clear()
            os.environ.update(keep)
        env = seen["env"]
        self.assertNotIn("CLAUDECODE", env)
        self.assertNotIn("CLAUDE_CODE_SESSION_ID", env)
        self.assertNotIn("ORCHD_ORCH_ID", env)
        self.assertEqual(env["ORCHD_HOME"], "/x")


class MigrationTest(unittest.TestCase):
    V1_TASKS = """CREATE TABLE tasks(id TEXT PRIMARY KEY, repo TEXT NOT NULL, repo_path TEXT NOT NULL,
        title TEXT NOT NULL, instructions TEXT NOT NULL, done_when TEXT NOT NULL, orch_thread TEXT NOT NULL,
        codex_bin TEXT NOT NULL, base TEXT, branch TEXT, worktree TEXT, socket TEXT, job_id TEXT,
        session_id TEXT, status TEXT NOT NULL, note TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL)"""

    def test_v1_database_gains_new_columns_and_keeps_rows(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "v1.db"
            old = sqlite3.connect(path)
            old.execute(self.V1_TASKS)
            old.execute("INSERT INTO tasks VALUES('abc','r','/p','T','i','d','th','/codex',NULL,NULL,NULL,NULL,"
                        "NULL,NULL,'acked',NULL,1,1)")
            old.commit()
            old.close()
            con = store.connect(path)
            cols = {r["name"] for r in con.execute("PRAGMA table_info(tasks)")}
            self.assertTrue({"model", "model_reason", "task_type", "rework_of", "outcome", "rating"} <= cols)
            self.assertEqual(store.get_task(con, "abc")["status"], "acked")
            store.connect(path)  # second connect is a no-op


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
                "arguments": {"repo": "demo", "title": "T", "instructions": "i", "done_when": "d",
                              "model_reason": "r", "task_type": "code"}}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "list_open", "arguments": {}}},
        )
        self.assertEqual([r["id"] for r in replies], [0, 1, 2, 3])
        self.assertEqual({t["name"] for t in replies[1]["result"]["tools"]},
                         {"dispatch", "inbox", "list_open", "list_orchs", "answer", "close", "view_worker", "lock_verify", "retry", "followup",
                      "entry_inbox", "send_to_nat", "ask_nat"})
        self.assertNotIn("isError", replies[2]["result"])
        (row,) = json.loads(replies[3]["result"]["content"][0]["text"])
        self.assertEqual(row["orch_thread"], "thread-X")
        dispatched = json.loads(replies[2]["result"]["content"][0]["text"])
        self.assertEqual((dispatched["orch_id"], dispatched["model"], dispatched["other_open_on_repo"]),
                         ("thread-X", "gpt-6.1-sol", []))

    def test_env_orch_id_wins_over_meta_thread(self):
        os.environ["ORCHD_ORCH_ID"] = "oabc1234"
        self.addCleanup(os.environ.pop, "ORCHD_ORCH_ID", None)
        replies = self.run_server({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
            "name": "dispatch", "_meta": {"threadId": "thread-Z"},
            "arguments": {"repo": "demo", "title": "T", "instructions": "i", "done_when": "d",
                              "model_reason": "r", "task_type": "code"}}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "list_open", "arguments": {}}})
        (row,) = json.loads(replies[1]["result"]["content"][0]["text"])
        self.assertEqual(row["orch_thread"], "oabc1234")

    def test_codex_thread_is_registered_lazily_as_codex(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        con = store.connect(Path(tmp.name) / "t.db")
        mcp_server.serve(io.StringIO(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "list_open", "_meta": {"threadId": "thread-C"}, "arguments": {}}}) + "\n"),
            io.StringIO(), con, FakeRuntime())
        self.assertEqual(store.get_orch(con, "thread-C")["kind"], "codex")

    def test_tool_errors_are_reported_not_raised(self):
        (reply,) = self.run_server({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {
            "name": "dispatch", "_meta": {"threadId": "t"},
            "arguments": {"repo": "missing", "title": "T", "instructions": "i", "done_when": "d",
                          "model_reason": "r", "task_type": "code"}}})
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

    def test_pushed_without_upstream_is_removable(self):
        self.commit()
        self.git("push", "-q", "origin", self.branch, cwd=self.wt)
        self.assertEqual(self.rt.worktree_state(self.wt, self.base), (True, "pushed"))

    def test_pushed_branch_is_removable(self):
        self.commit()
        self.git("push", "-q", "-u", "origin", self.branch, cwd=self.wt)
        self.assertEqual(self.rt.worktree_state(self.wt, self.base), (True, "pushed"))

    def test_repo_must_be_direct_child_of_projects(self):
        with self.assertRaises(ValueError):
            self.rt.repo_path("../etc")


if __name__ == "__main__":
    unittest.main()
