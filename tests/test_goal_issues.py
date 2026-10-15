import contextlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchd import cli, goals, issue_links, mcp_server, snapshot, store


class GoalIssuesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "orchd.db"
        self.con = store.connect(self.path)
        self.addCleanup(self.con.close)

    def test_migration_failure_rolls_back_and_reopen_upgrades_idempotently(self):
        # A synthetic pre-feature DB; only this additive table/index may change.
        self.con.execute("DROP INDEX IF EXISTS goal_issues_active_order")
        self.con.execute("DROP TABLE IF EXISTS goal_issues")
        goal = goals.set_goal(self.con, actor="operator", repo="example")
        before = self.con.execute("SELECT type,name,sql FROM sqlite_master ORDER BY name").fetchall()
        before_goal = goals.get(self.con, goal["id"], history=True)
        backup_path = Path(self.temp.name) / "backup.db"
        with contextlib.closing(sqlite3.connect(backup_path)) as backup:
            self.con.backup(backup)
        broken = store.SCHEMA.replace("CREATE INDEX IF NOT EXISTS goal_issues_active_order",
                                      "INVALID SQL; CREATE INDEX IF NOT EXISTS goal_issues_active_order")
        with patch.object(store, "SCHEMA", broken), self.assertRaises(sqlite3.OperationalError):
            store.connect(self.path)
        self.assertEqual(self.con.execute("SELECT type,name,sql FROM sqlite_master ORDER BY name").fetchall(), before)
        for _ in range(2):
            upgraded = store.connect(self.path)
            try:
                self.assertEqual(upgraded.execute("SELECT COUNT(*) FROM goal_issues").fetchone()[0], 0)
                self.assertEqual(goals.get(upgraded, goal["id"])["repo"], "example")
            finally:
                upgraded.close()
        self.assertEqual(goals.get(self.con, goal["id"], history=True), before_goal)
        with contextlib.closing(sqlite3.connect(backup_path)) as restored:
            restored.row_factory = sqlite3.Row
            self.assertEqual(goals.get(restored, goal["id"], history=True), before_goal)
            self.assertEqual(restored.execute("SELECT type,name,sql FROM sqlite_master ORDER BY name").fetchall(), before)
        after = self.con.execute("SELECT type,name,sql FROM sqlite_master ORDER BY name").fetchall()
        self.assertEqual([r for r in after if not r[1].startswith(("goal_issues", "sqlite_autoindex_goal_issues"))], before)

    def test_ordered_membership_reorder_remove_readd_and_noop(self):
        goal = goals.set_goal(self.con, actor="operator", repo="example")
        refs = ["github:Example/Project#3", "jira:DEMO-2", "github:example/project#1"]
        goal = goals.set_goal(self.con, goal["id"], actor="orch:test", issues=refs,
                              expected_revision=goal["issue_scope"]["revision"])
        self.assertEqual(goal["issues"], [
            dict(issue_ref="github:example/project#3", position=0, resolution="unresolved"),
            dict(issue_ref="jira:DEMO-2", position=1, resolution="unresolved"),
            dict(issue_ref="github:example/project#1", position=2, resolution="unresolved")])
        self.assertEqual(goals.list_goals(self.con)[0]["issues"], goal["issues"])
        self.assertEqual(goal["history"][-1]["actor"], "orch:test")
        self.assertGreater(goal["history"][-1]["created_at"], 0)
        for ordered in ([refs[2], refs[0], refs[1]], [refs[2], refs[1]], refs):
            goal = goals.set_goal(self.con, goal["id"], actor="orch:test", issues=ordered,
                                  expected_revision=goal["issue_scope"]["revision"])
            self.assertEqual([x["issue_ref"] for x in goal["issues"]], [x.lower() if x.startswith("github:") else x for x in ordered])
            self.assertEqual([x["position"] for x in goal["issues"]], list(range(len(ordered))))
        unchanged = goals.set_goal(self.con, goal["id"], actor="orch:test", issues=refs,
                                   expected_revision=goal["issue_scope"]["revision"])
        self.assertEqual(unchanged, goal)
        second = goals.set_goal(self.con, actor="operator", repo="example", issues=refs)
        self.assertEqual(second["issues"], goal["issues"])
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM goal_issues WHERE goal_id=?", (goal["id"],)).fetchone()[0], 3)

    def test_mcp_goal_list_and_readonly_snapshot_cli_json(self):
        goal = mcp_server.call("goal_set", {"fields": {"repo": "example"}}, "caller", self.con, None)
        refs = ["github:Example/Project#3", "jira:DEMO-2", "github:example/project#1"]
        def replace(ordered):
            nonlocal goal
            goal = mcp_server.call("goal_set", {"goal_id": goal["id"], "expected_revision": goal["issue_scope"]["revision"],
                "fields": {"issues": ordered}}, "caller", self.con, None)
        replace(refs)
        self.assertEqual(mcp_server.call("goal_list", {}, "caller", self.con, None)[0]["issues"], goal["issues"])
        replace([refs[2], refs[0], refs[1]])
        replace([refs[2], refs[1]])
        replace([refs[2], refs[1]])  # reattaching is a no-op
        before = {suffix: Path(str(self.path) + suffix).read_bytes() for suffix in ("", "-wal", "-shm")}
        out = io.StringIO()
        with patch.dict(os.environ, ORCHD_HOME=self.temp.name), contextlib.redirect_stdout(out), patch(
                "orchd.store.connect", side_effect=AssertionError("reader must not migrate")):
            self.assertEqual(cli.main(["snapshot", "--json", "--schema-version", "2"]), 0)
        data = json.loads(out.getvalue())
        self.assertEqual(data["goal_issues"], dict(version=1, available=True, complete=True))
        self.assertEqual(data["goals"][0]["issues"], [
            dict(issue_ref="github:example/project#1", position=0, resolution="unresolved"),
            dict(issue_ref="jira:DEMO-2", position=1, resolution="unresolved")])
        self.assertEqual(data["goals"][0]["issue_scope"]["state"], "set")
        self.assertEqual(before, {suffix: Path(str(self.path) + suffix).read_bytes() for suffix in before})
        self.assertNotIn("issues", snapshot.read(self.path, schema_version=1)["goals"][0])

    def test_readonly_unsupported_unset_and_explicit_empty_are_distinct(self):
        goal = goals.set_goal(self.con, actor="operator", repo="example")
        self.assertEqual(snapshot.read(self.path, schema_version=2)["goals"][0]["issue_scope"]["state"], "unset")
        self.con.execute("DROP TABLE goal_issues")
        with patch("orchd.store.connect", side_effect=AssertionError("no migration")):
            data = snapshot.read(self.path, schema_version=2)
        self.assertEqual(data["goal_issues"], dict(version=1, available=False, complete=False))
        self.assertIsNone(data["goals"][0]["issues"])
        self.assertEqual(data["goals"][0]["issue_scope"]["state"], "unsupported")
        upgraded = store.connect(self.path)
        upgraded.close()
        empty = goals.set_goal(self.con, goal["id"], actor="operator", issues=[],
                               expected_revision=goal["issue_scope"]["revision"])
        self.assertEqual(empty["history"][-1]["changes"]["issues"], dict(before=None, after=[]))
        self.assertEqual(snapshot.read(self.path, schema_version=2)["goals"][0]["issue_scope"]["state"], "set")

    def test_invalid_input_stale_revision_and_audit_failure_leave_scope_unchanged(self):
        goal = goals.set_goal(self.con, actor="operator", repo="example", issues=["jira:DEMO-1"])
        rev = goal["issue_scope"]["revision"]
        for invalid in (None, "jira:DEMO-1", [None], ["bad"], ["github:a/b#0"],
                        ["github:Example/Project#1", "github:example/project#1"], ["jira:DEMO-1"] * 2):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                goals.set_goal(self.con, goal["id"], actor="operator", issues=invalid, pg="must rollback", expected_revision=rev)
        for token in (None, True, str(rev), rev - 1):
            with self.subTest(token=token), self.assertRaisesRegex(ValueError, "expected_revision"):
                goals.set_goal(self.con, goal["id"], actor="operator", issues=[], expected_revision=token)
        with self.assertRaises(KeyError):
            goals.set_goal(self.con, "missing", actor="operator", issues=[], expected_revision=rev)
        with self.assertRaises(ValueError):
            goals.set_goal(self.con, goal["id"], actor="", issues=[], expected_revision=rev)
        self.assertEqual(goals.list_goals(self.con)[0]["issues"], goal["issues"])
        self.con.execute("CREATE TRIGGER fail_audit BEFORE INSERT ON goal_history BEGIN SELECT RAISE(ABORT,'audit failed'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            goals.set_goal(self.con, goal["id"], actor="operator", issues=["jira:DEMO-2"], pg="rollback", expected_revision=rev)
        self.assertEqual(goals.get(self.con, goal["id"], history=True, include_issues=True), goal)
        self.con.execute("DROP TRIGGER fail_audit")
        other = store.connect(self.path)
        try:
            updated = goals.set_goal(other, goal["id"], actor="other", sprint_goal="new text")
        finally:
            other.close()
        self.assertEqual(updated["issues"], goal["issues"])
        with self.assertRaisesRegex(ValueError, "stale"):
            goals.set_goal(self.con, goal["id"], actor="operator", issues=[], expected_revision=rev)
        empty = goals.set_goal(self.con, goal["id"], actor="operator", issues=[], expected_revision=updated["issue_scope"]["revision"])
        again = goals.set_goal(self.con, goal["id"], actor="operator", issues=[], expected_revision=empty["issue_scope"]["revision"])
        self.assertEqual(empty, again)
        self.assertEqual(empty["history"][-1]["changes"]["issues"], dict(before=["jira:DEMO-1"], after=[]))

    def test_cli_mutations_accept_ordered_json_and_mcp_schema_advertises_contract(self):
        def run(*args):
            out = io.StringIO()
            with patch.dict(os.environ, ORCHD_HOME=self.temp.name), contextlib.redirect_stdout(out):
                code = cli.main(["goal", *args])
            self.assertEqual(code, 0)
            return json.loads(out.getvalue())
        goal = run("add", "example", "--issues", '["jira:DEMO-1"]', "--actor", "test")
        changed = run("set", goal["id"], "--issues", '[]', "--expected-revision", str(goal["issue_scope"]["revision"]))
        self.assertEqual(changed["issues"], [])
        self.assertEqual(run("show", goal["id"])["issue_scope"]["state"], "set")
        self.assertEqual(run("list")[0]["issues"], [])
        tool = next(t for t in mcp_server.TOOLS if t["name"] == "goal_set")
        self.assertIn("expected_revision", tool["inputSchema"]["properties"])
        self.assertEqual(tool["inputSchema"]["properties"]["fields"]["properties"]["issues"]["type"], "array")
        with self.assertRaises(ValueError):
            mcp_server.call("goal_set", {"goal_id": goal["id"], "fields": {"issues": []}}, None, self.con, None)

    def test_snapshot_preserves_tasks_outside_confirmed_scope_and_warns(self):
        goal = goals.set_goal(self.con, actor="operator", repo="example", issues=["jira:DEMO-1"])
        self.con.execute("INSERT INTO tasks(id,repo,repo_path,title,instructions,done_when,orch_thread,codex_bin,"
                         "status,created_at,updated_at,goal_id) VALUES('outside','example','/synthetic','task','i','d','o','c','done',1,2,?)",
                         (goal["id"],))
        issue_links.link(self.con, "outside", "github:Example/Project#2")
        data = snapshot.read(self.path, schema_version=2)
        self.assertEqual(data["goals"][0]["issue_scope_mismatches"], [dict(task_id="outside", issue_ref="github:example/project#2")])
        self.assertEqual(data["goals"][0]["linked_tasks"][0]["task_id"], "outside")
        self.assertEqual(data["tasks"][0]["id"], "outside")
        self.assertEqual(goals.list_goals(self.con)[0]["issues"], goal["issues"])

    def test_legacy_goal_without_history_can_initialize_scope(self):
        self.con.execute("INSERT INTO goals(id,repo,created_at,updated_at) VALUES('legacy','example',1,2)")
        old = goals.list_goals(self.con)[0]
        self.assertEqual(old["issue_scope"]["revision"], 0)
        initialized = goals.set_goal(self.con, "legacy", actor="operator", issues=[], expected_revision=0)
        self.assertEqual(initialized["issue_scope"]["state"], "set")

    def test_soft_removal_readd_preserves_identity_and_creation_timestamp(self):
        goal = goals.set_goal(self.con, actor="operator", repo="example", issues=["jira:DEMO-1", "jira:DEMO-2"])
        created = self.con.execute("SELECT created_at FROM goal_issues WHERE issue_ref='jira:DEMO-1'").fetchone()[0]
        removed = goals.set_goal(self.con, goal["id"], actor="operator", issues=["jira:DEMO-2"],
                                 expected_revision=goal["issue_scope"]["revision"])
        row = self.con.execute("SELECT * FROM goal_issues WHERE issue_ref='jira:DEMO-1'").fetchone()
        self.assertIsNotNone(row["removed_at"])
        self.assertEqual(removed["history"][-1]["changes"]["issues"],
                         dict(before=["jira:DEMO-1", "jira:DEMO-2"], after=["jira:DEMO-2"]))
        goals.set_goal(self.con, goal["id"], actor="operator", issues=["jira:DEMO-1", "jira:DEMO-2"],
                       expected_revision=removed["issue_scope"]["revision"])
        row = self.con.execute("SELECT * FROM goal_issues WHERE issue_ref='jira:DEMO-1'").fetchone()
        self.assertIsNone(row["removed_at"])
        self.assertEqual(row["created_at"], created)
