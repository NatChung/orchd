import contextlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

from orchd import board, cli, core, goals, mcp_server, store
from tests.test_orchd import FakeRuntime


class GoalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.con = store.connect(self.root / "orchd.db")
        self.addCleanup(self.con.close)
        self.rt = FakeRuntime()

    def goal(self, **fields):
        return goals.set_goal(self.con, actor="Operator", repo=fields.pop("repo", "orchd"), **fields)

    def task(self, **fields):
        defaults = dict(orch_thread="orch-one", repo="orchd", title="test task", instructions="bounded",
                        done_when="tested", model_reason="test", task_type="code")
        return core.dispatch(self.con, self.rt, **(defaults | fields))

    def test_additive_legacy_migration_is_idempotent(self):
        old_path = self.root / "legacy.db"
        old = sqlite3.connect(old_path)
        # v1 tasks schema only; a populated old row survives both migrations untouched.
        old.execute(store.SCHEMA.split(";")[0])
        old.execute("INSERT INTO tasks(id,repo,repo_path,title,instructions,done_when,orch_thread,codex_bin,"
                    "status,created_at,updated_at) VALUES('old','orchd','/repo','old title','i','d','o','c','done',1,2)")
        old.commit()
        old.close()
        for iteration in range(2):
            con = store.connect(old_path)
            try:
                task = store.get_task(con, "old")
                self.assertEqual((task["title"], task["status"], task["updated_at"]), ("old title", "done", 2))
                self.assertIsNone(task["goal_id"])
                self.assertIsNone(task["goal_critical"])
                self.assertEqual(con.execute("SELECT COUNT(*) FROM goal_history").fetchone()[0], iteration)
                goals.set_goal(con, actor="test", repo="orchd")
            finally:
                con.close()

    def test_history_records_before_after_actor_time_and_noop(self):
        goal = self.goal(v=7, companies=["ExampleCompany"], sprint_goal="ship")
        self.assertEqual(goal["history"][0]["actor"], "Operator")
        self.assertGreater(goal["history"][0]["created_at"], 0)
        changed = goals.set_goal(self.con, goal["id"], actor="orch:test", status="waiting", ball="Operator")
        self.assertEqual(changed["history"][-1]["changes"]["status"], {"before": "active", "after": "waiting"})
        unchanged = goals.set_goal(self.con, goal["id"], actor="orch:test", status="waiting", companies=["ExampleCompany"])
        self.assertEqual(len(unchanged["history"]), 2)
        self.assertEqual(unchanged["updated_at"], changed["updated_at"])

    def test_history_failure_rolls_back_goal_and_links(self):
        goal = self.goal(pg="before")
        task = self.task()
        self.con.execute("CREATE TRIGGER fail_audit BEFORE INSERT ON goal_history BEGIN SELECT RAISE(ABORT,'audit failed'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            goals.set_goal(self.con, goal["id"], actor="test", pg="after",
                           linked_tasks=[dict(task_id=task["id"], goal_critical=True)])
        self.assertEqual(goals.get(self.con, goal["id"])["pg"], "before")
        self.assertIsNone(store.get_task(self.con, task["id"])["goal_id"])

    def test_validation_prevents_partial_updates(self):
        goal = self.goal(sprint_start="2026-10-05", sprint_end="2026-10-11")
        for fields in (dict(v=11), dict(v=True), dict(v=float("nan")), dict(j=4), dict(j=True),
                       dict(status="blocked"), dict(type="project"), dict(deadline="10/7"),
                       dict(deadline="2026-02-30"), dict(companies="ExampleCompany"), dict(unknown="x"),
                       dict(sprint_start="2026-10-12"), dict(repo="different"), dict(pg=None),
                       dict(linked_tasks=[dict(task_id="x", goal_critical="yes")])):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                goals.set_goal(self.con, goal["id"], actor="test", **fields)
        self.assertEqual(len(goals.get(self.con, goal["id"], history=True)["history"]), 1)
        with self.assertRaises(ValueError):
            self.goal(repo="/repo/path")
        with self.assertRaises(KeyError):
            goals.set_goal(self.con, "missing", actor="test", pg="x")

    def test_dispatch_optional_link_and_audit(self):
        legacy = self.task()
        self.assertIsNone(legacy["goal_id"])
        goal = self.goal(v=8)
        task = self.task(goal_id=goal["id"], goal_critical=True)
        self.assertEqual(task["goal_id"], goal["id"])
        output = core.list_open(self.con, self.rt)
        self.assertEqual(next(t for t in output if t["task_id"] == task["id"])["goal_critical"], 1)
        history = goals.get(self.con, goal["id"], history=True)["history"]
        self.assertEqual(history[-1]["actor"], "orch:orch-one")
        self.assertEqual(history[-1]["changes"]["linked_tasks"]["after"][0]["task_id"], task["id"])

    def test_link_validation_and_replacement(self):
        goal = self.goal()
        for fields in (dict(goal_id="missing"), dict(goal_id=goal["id"], repo="other"),
                       dict(goal_critical=True), dict(goal_id=goal["id"], goal_critical="yes")):
            with self.subTest(fields=fields), self.assertRaises((ValueError, KeyError)):
                self.task(**fields)
        self.assertEqual(len(store.open_tasks(self.con)), 0)
        task = self.task()
        goals.set_goal(self.con, goal["id"], actor="Operator", linked_tasks=[dict(task_id=task["id"], goal_critical=False)])
        self.assertEqual(store.get_task(self.con, task["id"])["goal_critical"], 0)
        other = self.goal()
        with self.assertRaises(ValueError):
            goals.set_goal(self.con, other["id"], actor="test", linked_tasks=[dict(task_id=task["id"], goal_critical=True)])
        goals.set_goal(self.con, goal["id"], actor="Operator", linked_tasks=[])
        self.assertIsNone(store.get_task(self.con, task["id"])["goal_id"])

    def test_scoring_formula_clamps_unknown_and_progress(self):
        today = date(2026, 10, 6)
        goal = self.goal(v=8, j=2, deadline="2026-10-08", last_progress_date="2026-09-01",
                         ball="Operator", waiting_nat_since="2026-10-03")
        self.assertEqual(goals.score(goal, today), dict(V=8, T=8, S=10, B=3, J=2, score=14.5))
        self.assertEqual(goals.score(dict(goal, ball="nat"), today), goals.score(goal, today))
        self.assertEqual(goals.score(goal, today, progress_date="2026-10-05")["S"], 1)
        self.assertEqual(goals.score(goal | dict(deadline="2026-10-01"), today)["T"], 10)
        self.assertEqual(goals.score(goal | dict(deadline="2026-11-01", last_progress_date="2026-10-07"), today)["S"], 0)
        self.assertEqual(goals.score(goal | dict(ball="customer"), today)["B"], 0)
        self.assertEqual(goals.score(goal | dict(v=None), today)["score"], None)
        self.assertEqual(goals.score(goal | dict(ball="customer"), today, nat_since="2026-09-01")["B"], 10)

    def command(self, argv):
        output, errors = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, ORCHD_HOME=str(self.root)), patch("orchd.cli.Runtime", return_value=self.rt), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = cli.main(argv)
        return code, output.getvalue(), errors.getvalue()

    def test_cli_add_set_show_list_export_and_errors(self):
        code, text, _ = self.command(["goal", "add", "orchd", "--pg", "central PG", "--v", "7", "--actor", "Operator"])
        self.assertEqual(code, 0)
        goal_id = json.loads(text)["id"]
        self.assertEqual(self.command(["goal", "set", goal_id, "--fields", '{"v":null,"status":"paused"}'])[0], 0)
        result = json.loads(self.command(["goal", "show", goal_id])[1])
        self.assertIsNone(result["v"])
        self.assertEqual(len(result["history"]), 2)
        self.assertEqual(len(json.loads(self.command(["goal", "list", "--repo", "orchd", "--status", "paused"])[1])), 1)
        code, text, _ = self.command(["goal", "export", "--md"])
        self.assertEqual(code, 0)
        self.assertIn("central PG", text)
        self.assertIn("Change history", text)
        self.assertIn("Operator", text)
        self.assertEqual(self.command(["goal", "set", goal_id, "--j", "4"])[0], 1)
        self.assertEqual(self.command(["goal", "add", "orchd", "--fields", '[]'])[0], 1)
        self.assertEqual(self.command(["goal", "show", "missing"])[0], 1)

    def test_mcp_goal_tools_orch_only_and_dispatch_params(self):
        result = mcp_server.call("goal_set", {"fields": dict(repo="orchd", v=9)}, "orch-one", self.con, self.rt)
        self.assertEqual(result["history"][0]["actor"], "orch:orch-one")
        self.assertEqual(len(mcp_server.call("goal_list", {}, None, self.con, self.rt)), 1)
        with self.assertRaises(ValueError):
            mcp_server.call("goal_set", {"fields": dict(repo="orchd")}, None, self.con, self.rt)
        with self.assertRaises(PermissionError):
            mcp_server.entry_call("goal_set", {}, {}, self.con, self.rt, "desktop")
        props = next(t for t in mcp_server.TOOLS if t["name"] == "dispatch")["inputSchema"]["properties"]
        self.assertIn("goal_id", props)
        self.assertIn("goal_critical", props)
        args = dict(repo="orchd", title="linked", instructions="scope", done_when="tests",
                    model_reason="test", task_type="code", goal_id=result["id"], goal_critical=True)
        task = mcp_server.call("dispatch", args, "orch-one", self.con, self.rt)
        self.assertEqual(store.get_task(self.con, task["task_id"])["goal_id"], result["id"])

    def question(self, task=None, **extra):
        fields = dict(entry_id="desktop", orch_id="orch-one", direction="out", kind="question", body="Operator decide",
                      body_bytes=10, body_sha256="hash", delivery="held", created_at=1,
                      question_state="current", task_id=task["id"] if task else None)
        fields.update(extra)
        self.con.execute(f"INSERT INTO entry_messages({','.join(fields)}) VALUES({','.join('?' for _ in fields)})", tuple(fields.values()))

    def test_board_reads_questions_without_consuming_or_delivering(self):
        goal = self.goal(v=8, last_progress_date="2026-09-01")
        task = self.task(goal_id=goal["id"], goal_critical=True)
        self.question(task)
        self.question(task, body="already answered", question_state="answered")
        self.question(body="unassigned question", question_state="queued")
        mid = store.add_message(self.con, task["id"], "progress", "actual progress")
        before = self.con.total_changes
        snap = board.snapshot(self.con, self.rt)
        self.assertEqual(self.con.total_changes, before)
        self.assertIsNone(self.con.execute("SELECT read_at FROM messages WHERE id=?", (mid,)).fetchone()[0])
        project = next(p for p in snap["projects"] if p["repo"] == "orchd")
        self.assertEqual(len(project["questions"]), 1)
        self.assertEqual(project["goals"][0]["priority"]["S"], 0)
        self.assertEqual(project["goals"][0]["priority"]["B"], 10)
        self.assertEqual(next(p for p in snap["projects"] if p["repo"] == "unassigned")["questions"][0]["state"], "queued")
        self.assertEqual(self.rt.woken, [])

    def test_board_scored_project_tabs_collapses_escaping_and_done(self):
        goal = self.goal(v=9, sprint_goal="<script>alert(1)</script>", pg="small PG", last_progress_date=None)
        self.goal(repo="lower", v=2, last_progress_date=None)
        self.goal(repo="paused", status="paused", v=10, last_progress_date=None)
        critical = self.task(goal_id=goal["id"], goal_critical=True)
        self.question(critical)
        self.task(goal_id=goal["id"], goal_critical=False, title="secondary")
        done = self.task(goal_id=goal["id"], goal_critical=True, title="reported result")
        store.update_task(self.con, done["id"], status="done")
        blocked = self.task(title="unlinked blocker")
        store.update_task(self.con, blocked["id"], status="blocked")
        snap = board.snapshot(self.con, self.rt, date(2026, 10, 6))
        self.assertEqual([p["repo"] for p in snap["projects"]], ["orchd", "lower", "paused"])
        html = board.render(snap)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertNotIn("fonts.googleapis.com", html)
        self.assertIn('class="small-goals"', html)
        self.assertIn('role="tablist"', html)
        self.assertIn("ArrowRight", html)
        self.assertIn("已回報待核對", html)
        parser = DetailsParser()
        parser.feed(html)
        by_summary = dict(parser.details)
        self.assertTrue(by_summary["要你決定 · 目標關鍵 (1)"])
        self.assertTrue(by_summary["Blocked (1)"])
        self.assertFalse(by_summary["可追蹤紀錄"])
        self.assertFalse(by_summary["Done · 已回報待核對"])
        self.assertFalse(by_summary["暫停專案"])
        self.assertLess(html.index("small PG"), html.index("要你決定"))

    def test_read_cli_does_not_touch_source_db_or_inbox(self):
        self.goal(v=8)
        task = self.task()
        store.add_message(self.con, task["id"], "question", "unread")
        self.con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        def files():
            return {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.glob("orchd.db*") if p.is_file()}
        before = files()
        for argv in (["goal", "list"], ["goal", "export", "--md"], ["board", "--html", str(self.root / "board.html")]):
            self.assertEqual(self.command(argv)[0], 0)
        self.assertEqual(before, files())
        self.assertIn("Orch 任務看板", (self.root / "board.html").read_text())

    def test_empty_and_old_board_snapshot(self):
        self.assertIn("尚無目標或未結任務", board.render(board.snapshot(self.con, self.rt)))
        # Model opt-in: legacy tasks remain visible without inventing a goal or Operator decision.
        task = self.task()
        store.update_task(self.con, task["id"], status="question")
        store.add_message(self.con, task["id"], "question", "ask Orch, not Operator")
        snap = board.snapshot(self.con, self.rt)
        self.assertEqual(snap["projects"][0]["questions"], [])
        self.assertIn("Later", board.render(snap))

    def test_board_cli_migrates_only_copy_of_legacy_database(self):
        old_home = self.root / "old-home"
        old_home.mkdir()
        old = sqlite3.connect(old_home / "orchd.db")
        old.execute(store.SCHEMA.split(";")[0])
        old.commit()
        old.close()
        before = (old_home / "orchd.db").read_bytes()
        with patch.dict(os.environ, ORCHD_HOME=str(old_home)), patch("orchd.cli.Runtime", return_value=self.rt), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["board", "--html", str(self.root / "old-board.html")]), 0)
        self.assertEqual(before, (old_home / "orchd.db").read_bytes())
        self.assertEqual(sorted(p.name for p in old_home.iterdir()), ["orchd.db"])
        self.assertIn("尚無目標或未結任務", (self.root / "old-board.html").read_text())

    def test_failed_dispatch_audit_never_launches_or_creates_task(self):
        goal = self.goal()
        self.con.execute("CREATE TRIGGER fail_audit BEFORE INSERT ON goal_history BEGIN SELECT RAISE(ABORT,'audit failed'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.task(goal_id=goal["id"], goal_critical=True)
        self.assertEqual(len(store.open_tasks(self.con)), 0)
        self.assertEqual(goals.get(self.con, goal["id"])["linked_tasks"], [])


class DetailsParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack, self.details = [], []
        self.in_summary = False
        self.text = ""

    def handle_starttag(self, tag, attrs):
        if tag == "details":
            self.stack.append("open" in dict(attrs))
        if tag == "summary":
            self.in_summary, self.text = True, ""

    def handle_data(self, text):
        if self.in_summary:
            self.text += text

    def handle_endtag(self, tag):
        if tag == "summary":
            self.details.append((self.text, self.stack[-1]))
            self.in_summary = False
        if tag == "details":
            self.stack.pop()
