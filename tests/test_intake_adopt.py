"""#62: received intake receipts follow an Orch replacement; reconcile, self-claim and backlog visibility."""
import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from orchd import cli, core, intake, intake_decide, inventory, mcp_server, orch_restart, store
from orchd.runtime import ORCH_MODELS
from tests.test_intake import PRODUCERS, fixture
from tests.test_orchd import FakeRuntime


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "orchd.db"
        self.con = store.connect(self.path)
        self.addCleanup(lambda: self.con.close())
        self.rt = FakeRuntime()
        self.rt.jobs = {"newjob": {"pid": 4242, "status": "idle"}}  # oldjob absent = confirmed dead
        store.register_orch(self.con, "old", "claude", socket="/s/old", session_id="so", job_id="oldjob")
        store.register_orch(self.con, "new", "claude", socket="/s/new", session_id="sn", job_id="newjob")
        self.n = 0

    def receipt(self, owner=None, v2=False, **changes):
        self.n += 1
        payload = dict(fixture("event_v2_email.json" if v2 else "event_email.json"), event_id=f"mail:t:{self.n}", source_ref=f"mail:a/{self.n}", **changes)
        if owner:
            payload["owner_orch"] = owner
        return intake.submit(self.con, payload, producer="email-adapter", producers=PRODUCERS)["receipt_id"]

    def owner(self, rid):
        return self.con.execute("SELECT owner_orch FROM intake_events WHERE receipt_id=?", (rid,)).fetchone()[0]

    def notes(self, rid):
        return [(r["owner_orch"], r["state"]) for r in self.con.execute(
            "SELECT owner_orch,state FROM intake_notifications WHERE receipt_id=? ORDER BY id", (rid,))]

    def log_kinds(self):
        return [r["kind"] for r in self.con.execute("SELECT kind FROM intake_log ORDER BY seq")]

    def task(self, owner="old"):
        t = core.dispatch(self.con, self.rt, orch_thread=owner, repo="demo", title="T", instructions="x",
                          done_when="y", model="sonnet", model_reason="r", task_type="code")
        self.rt.sent.clear()
        return t["id"]


class AdoptMovesReceipts(Base):
    def test_adopt_moves_received_receipts_and_their_notifications(self):
        received, acked, excluded, other = (self.receipt("old", v2=True), self.receipt("old"), self.receipt("old"),
                                            self.receipt("new"))
        intake.ack(self.con, [acked])
        intake.exclude(self.con, [excluded], reason="noise", rule_version="r1", actor="orch")
        tid = self.task()
        out = core.adopt(self.con, self.rt, "new", [tid])
        self.assertEqual(out["intake_receipts"], [received])
        self.assertEqual(self.owner(received), "new")
        self.assertEqual(self.notes(received), [("old", "withdrawn"), ("new", "pending")])
        self.assertEqual(self.owner(acked), "old")
        self.assertEqual(self.owner(excluded), "old")
        self.assertEqual(self.owner(other), "new")
        self.assertEqual(self.log_kinds().count("adopt-bind"), 1)

    def test_adopt_by_task_id_moves_receipts_of_the_tasks_owner(self):
        rid = self.receipt("old")
        core.adopt(self.con, self.rt, "new", [self.task()])
        self.assertEqual(self.owner(rid), "new")

    def test_failed_adopt_leaves_receipts_untouched(self):
        rid = self.receipt("old")
        tid = self.task()
        with patch("orchd.intake.adopt_receipts", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                core.adopt(self.con, self.rt, "new", [tid])
        self.assertEqual(self.owner(rid), "old")
        self.assertEqual(store.get_task(self.con, tid)["orch_thread"], "old")

    def test_adopt_from_orch_without_open_tasks_still_moves_receipts(self):
        rid = self.receipt("old")
        out = core.adopt(self.con, self.rt, "new", from_orch="old")
        self.assertEqual((out["adopted"], out["intake_receipts"]), ([], [rid]))
        self.assertEqual(self.owner(rid), "new")
        self.assertEqual(self.rt.sent, [])  # nothing to wake the new Orch about as a task

    def test_zero_task_adopt_of_a_live_owner_is_refused_without_force(self):
        self.rt.jobs["oldjob"] = {"pid": 1, "status": "busy"}
        rid = self.receipt("old")
        with self.assertRaisesRegex(ValueError, "refusing to adopt"):
            core.adopt(self.con, self.rt, "new", from_orch="old")
        self.assertEqual(self.owner(rid), "old")
        self.assertEqual(core.adopt(self.con, self.rt, "new", from_orch="old", force=True)["intake_receipts"], [rid])

    def test_adopt_from_orch_with_nothing_at_all_is_still_an_error(self):
        with self.assertRaisesRegex(ValueError, "nothing to adopt"):
            core.adopt(self.con, self.rt, "new", from_orch="old")


class RestartMovesReceipts(Base):
    def setUp(self):
        super().setUp()
        self.rt.jobs = {"oldjob": dict(pid=4242, status="busy")}
        self.rt.stop_worker = Mock(side_effect=self.stop)
        self.rt.start_orch = Mock(side_effect=self.start)

    def stop(self, job):
        self.rt.jobs.pop(job, None)
        self.rt.dead_sockets = {"/s/old"}

    def start(self, orch_id, model, home):
        self.rt.jobs["j2"] = dict(pid=4243, status="idle")
        return "/fake/new2", "j2", "s2"

    def test_restart_without_open_tasks_moves_received_receipts(self):
        store.register_orch(self.con, "olds", "claude", model=ORCH_MODELS["sonnet"], socket="/s/old",
                            session_id="so", job_id="oldjob")
        rid = self.receipt("olds")
        out = orch_restart.restart(self.con, self.rt, old_id="olds")
        self.assertEqual(out["status"], "done", out)
        self.assertEqual(out["adopt"]["intake_receipts"], [rid])
        self.assertEqual(self.owner(rid), out["new_orch"])
        self.assertEqual(self.notes(rid)[-1], (out["new_orch"], "pending"))

    def test_restart_plan_mentions_adopt_when_receipts_are_waiting(self):
        store.register_orch(self.con, "olds", "claude", model=ORCH_MODELS["sonnet"], socket="/s/old",
                            session_id="so", job_id="oldjob")
        self.receipt("olds")
        out = orch_restart.restart(self.con, self.rt, old_id="olds", dry_run=True)
        self.assertTrue(any("adopt" in step and "skip" not in step for step in out["plan"]), out["plan"])


class IntakeReconcile(Base):
    def stopped_old(self):
        self.con.execute("UPDATE orchs SET stopped_at=1 WHERE id='old'")

    def bind_desktop(self):
        self.con.execute("INSERT INTO entries(id,orch_id,bound_at) VALUES('desktop','new',0)")

    def reconcile(self, **kw):
        return intake.reconcile_path(self.path, backup_dir=Path(self.tmp.name) / "b", jobs=self.rt.jobs, **kw)

    def test_dry_run_lists_and_writes_nothing(self):
        self.bind_desktop()
        self.stopped_old()
        rid, unassigned = self.receipt("old"), self.receipt()
        before = self.path.read_bytes()
        out = self.reconcile(apply=False)
        self.assertEqual(out["moved"], [rid])
        self.assertEqual((out["applied"], out["to_orch"]), (False, "new"))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse((Path(self.tmp.name) / "b").exists())
        self.assertIsNone(self.owner(unassigned))

    def test_apply_backs_up_first_then_moves_only_dead_or_stopped_owners(self):
        self.bind_desktop()
        self.stopped_old()
        dead_rid, unassigned, live = self.receipt("old", v2=True), self.receipt(), self.receipt("new")
        out = self.reconcile(apply=True)
        self.assertEqual(out["moved"], [dead_rid])
        self.assertTrue(out["applied"])
        backup = Path(out["backup"])
        self.assertTrue(backup.exists() and backup.name.endswith("-pre-intake-reconcile.db"))
        old = sqlite3.connect(backup)
        self.assertEqual(old.execute("SELECT owner_orch FROM intake_events WHERE receipt_id=?", (dead_rid,)).fetchone()[0], "old")
        old.close()
        self.con.close()
        self.con = store.connect(self.path)
        self.assertEqual(self.owner(dead_rid), "new")
        self.assertEqual(self.notes(dead_rid), [("old", "withdrawn"), ("new", "pending")])
        self.assertIsNone(self.owner(unassigned))
        self.assertEqual(self.owner(live), "new")
        self.assertEqual(self.log_kinds().count("adopt-bind"), 1)
        self.assertEqual(self.reconcile(apply=True)["moved"], [])  # idempotent

    def test_dead_by_probe_without_stopped_at_counts(self):
        self.bind_desktop()
        rid = self.receipt("old")  # oldjob is not in jobs -> dead
        self.assertEqual(self.reconcile(apply=False)["moved"], [rid])

    def test_unknown_health_is_not_moved(self):
        self.bind_desktop()
        self.receipt("old")
        out = intake.reconcile_path(self.path, apply=False, backup_dir=None, jobs=None)
        self.assertEqual(out["moved"], [])

    def test_apply_refuses_when_the_bound_orch_is_not_live(self):
        self.con.execute("INSERT INTO entries(id,orch_id,bound_at) VALUES('desktop','old',0)")
        self.stopped_old()
        rid = self.receipt("old")
        with self.assertRaisesRegex(ValueError, "not live"):
            self.reconcile(apply=True)
        self.assertEqual(self.owner(rid), "old")

    def test_no_binding_is_an_error_only_when_there_is_something_to_move(self):
        self.stopped_old()
        self.assertEqual(self.reconcile(apply=True)["moved"], [])
        self.receipt("old")
        with self.assertRaisesRegex(ValueError, "binding"):
            self.reconcile(apply=True)

    def test_cli_dry_run_does_not_create_or_migrate_the_database(self):
        missing = Path(self.tmp.name) / "empty-home"
        with patch.dict("os.environ", {"ORCHD_HOME": str(missing)}), \
                patch("orchd.home_migrations.ensure_current", return_value=[]):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cli.main(["intake", "reconcile"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["moved"], [])
        self.assertFalse((missing / "orchd.db").exists())

    def test_home_policy_dry_run_warns_and_apply_writes(self):
        parser = cli.build_parser()
        self.assertEqual(cli.HOME_POLICY[("intake", "reconcile")], "write")
        self.assertEqual(cli._home_policy(parser.parse_args(["intake", "reconcile"])), "warn")
        self.assertEqual(cli._home_policy(parser.parse_args(["intake", "reconcile", "--apply"])), "write")


class SelfAssign(Base):
    def test_takes_unassigned_for_the_caller_with_a_notification(self):
        a, b = self.receipt(), self.receipt()
        out = intake.take_unassigned(self.con, [a, b], "new")
        self.assertEqual([r["status"] for r in out["receipts"]], ["bound", "bound"])
        self.assertEqual(self.owner(a), "new")
        self.assertEqual(self.notes(a), [("new", "pending")])
        self.assertEqual(self.log_kinds().count("bind"), 2)

    def test_rejects_owned_by_someone_else_and_is_atomic(self):
        free, taken = self.receipt(), self.receipt("old")
        with self.assertRaises(intake.IntakeError) as caught:
            intake.take_unassigned(self.con, [free, taken], "new")
        self.assertEqual(caught.exception.code, "not_owner")
        self.assertIsNone(self.owner(free))
        self.assertEqual(self.owner(taken), "old")

    def test_own_and_non_received_receipts(self):
        mine = self.receipt("new")
        self.assertEqual(intake.take_unassigned(self.con, [mine], "new")["receipts"][0]["status"], "unchanged")
        done = self.receipt()
        intake.exclude(self.con, [done], reason="noise", rule_version="r1", actor="orch")
        with self.assertRaises(intake.IntakeError) as caught:
            intake.take_unassigned(self.con, [done], "new")
        self.assertEqual(caught.exception.code, "invalid_state")

    def test_unregistered_caller_and_empty_batch_are_rejected(self):
        rid = self.receipt()
        with self.assertRaises(intake.IntakeError) as caught:
            intake.take_unassigned(self.con, [rid], "ghost")
        self.assertEqual(caught.exception.code, "unknown_owner")
        with self.assertRaises(intake.IntakeError):
            intake.take_unassigned(self.con, [], "new")

    def test_mcp_tool_binds_only_for_the_caller(self):
        tool = next(t for t in mcp_server.TOOLS if t["name"] == "intake_take_unassigned")
        self.assertFalse(tool["annotations"]["readOnlyHint"])
        self.assertEqual(set(tool["inputSchema"]["properties"]), {"receipt_ids"})
        rid = self.receipt()
        out = mcp_server.call("intake_take_unassigned", dict(receipt_ids=[rid], owner_orch="old"), "new", self.con, self.rt)
        self.assertEqual(self.owner(rid), "new")
        self.assertEqual(out["status"], "bound")

    def test_decide_works_after_taking(self):
        rid = self.receipt()
        intake.take_unassigned(self.con, [rid], "new")
        self.assertEqual(intake.pending(self.con, "new")["events"][0]["receipt_id"], rid)


class BindOnlyIfUnassigned(Base):
    """nat-assistant's polling runner looks at the unassigned queue, then binds: the bind must be a compare-and-set."""

    def test_binds_an_unassigned_receipt(self):
        rid = self.receipt(v2=True)
        out = intake.bind_owner(self.con, rid, "new", only_if_unassigned=True)
        self.assertEqual((out["status"], out["owner_orch"]), ("bound", "new"))
        self.assertEqual(self.notes(rid), [("new", "pending")])

    def test_taken_after_the_query_is_skipped_not_overwritten(self):
        rid = self.receipt(v2=True)
        stale = self.con.execute("SELECT * FROM intake_events WHERE receipt_id=?", (rid,)).fetchone()
        intake.bind_owner(self.con, rid, "old", only_if_unassigned=True)  # another Orch wins the race
        before = self.log_kinds()
        with patch("orchd.intake._event", return_value=stale):  # the caller's view is stale
            out = intake.bind_owner(self.con, rid, "new", only_if_unassigned=True)
        self.assertEqual(out["status"], "skipped")
        self.assertEqual(self.owner(rid), "old")
        self.assertEqual(self.notes(rid), [("old", "pending")])
        self.assertEqual(self.log_kinds(), before)

    def test_plain_bind_still_rebinds_for_the_operator(self):
        rid = self.receipt("old")
        self.assertEqual(intake.bind_owner(self.con, rid, "new")["status"], "bound")
        self.assertEqual(self.owner(rid), "new")

    def test_non_received_and_unknown_owner(self):
        rid = self.receipt()
        intake.exclude(self.con, [rid], reason="noise", rule_version="r1", actor="orch")
        self.assertEqual(intake.bind_owner(self.con, rid, "new", only_if_unassigned=True)["status"], "skipped")
        with self.assertRaises(intake.IntakeError) as caught:
            intake.bind_owner(self.con, self.receipt(), "ghost", only_if_unassigned=True)
        self.assertEqual(caught.exception.code, "unknown_owner")

    def test_mcp_take_loses_the_race_with_a_stale_view(self):
        rid = self.receipt()
        stale = self.con.execute("SELECT * FROM intake_events WHERE receipt_id=?", (rid,)).fetchone()
        intake.bind_owner(self.con, rid, "old", only_if_unassigned=True)
        with patch("orchd.intake._event", return_value=stale):
            with self.assertRaises(intake.IntakeError) as caught:
                intake.take_unassigned(self.con, [rid], "new")
        self.assertEqual(caught.exception.code, "not_owner")
        self.assertEqual(self.owner(rid), "old")

    def test_cli_flag_reports_bound_then_skipped(self):
        rid = self.receipt()
        results = []
        for owner in ("new", "old"):
            out = io.StringIO()
            with patch.dict("os.environ", {"ORCHD_HOME": self.tmp.name}), \
                    patch("orchd.home_migrations.ensure_current", return_value=[]), contextlib.redirect_stdout(out):
                code = cli.main(["intake", "bind", "--receipt", rid, "--owner", owner, "--only-if-unassigned"])
            results.append((code, json.loads(out.getvalue())["status"]))
        self.assertEqual(results, [(0, "bound"), (0, "skipped")])
        self.assertEqual(self.owner(rid), "new")


class BacklogVisibility(Base):
    def test_backlog_reports_unassigned_and_flags_dead_and_stopped_owner_queues(self):
        self.receipt()
        self.receipt("old")
        self.receipt("new")
        store.register_orch(self.con, "gone", "claude", socket="/s/g", session_id="sg", job_id="gonejob")
        self.con.execute("UPDATE orchs SET stopped_at=1 WHERE id='gone'")
        self.receipt("gone")
        out = intake_decide.backlog(self.con, owner="new", jobs=self.rt.jobs)
        queues = {q["owner"]: q for q in out["queues"]}
        self.assertEqual(out["unassigned"], 1)
        self.assertEqual(queues["new"]["owner_state"], "assigned")
        self.assertEqual(queues["old"]["owner_state"], "owner_dead")
        self.assertEqual(queues["gone"]["owner_state"], "owner_stopped")
        self.assertNotIn(None, queues)

    def test_without_a_runtime_snapshot_stopped_is_flagged_but_dead_is_not_guessed(self):
        self.receipt("old")
        self.con.execute("UPDATE orchs SET stopped_at=1 WHERE id='old'")
        store.register_orch(self.con, "other", "claude", socket="/s/o", session_id="s", job_id="oj")
        self.receipt("other")
        out = intake_decide.backlog(self.con, owner="new")
        self.assertEqual({q["owner"]: q["owner_state"] for q in out["queues"]}, {"old": "owner_stopped"})

    def test_cli_backlog_without_owner_keeps_the_unassigned_queue(self):
        self.receipt()
        out = intake_decide.backlog(self.con)
        self.assertEqual([(q["owner"], q["owner_state"]) for q in out["queues"]], [(None, "unassigned")])
        self.assertEqual(out["unassigned"], 1)

    def test_mcp_backlog_flags_dead_owner(self):
        self.receipt("old")
        out = mcp_server.call("intake_backlog", {}, "new", self.con, self.rt)
        self.assertEqual(out["queues"][0]["owner_state"], "owner_dead")


class InventoryCountsIntake(Base):
    def report(self):
        return inventory.list_orchs(self.con, self.rt, observe=False)

    def test_dead_orch_with_only_received_intake_has_obligations_and_warns(self):
        self.rt.dead_sockets = {"/s/old"}
        self.con.execute("UPDATE orchs SET stopped_at=1 WHERE id='old'")  # stopped, not merely retired
        self.receipt("old")
        old = next(r for r in self.report()["orchs"] if r["orch_id"] == "old")
        self.assertEqual(old["intake_received_count"], 1)
        self.assertTrue(old["has_obligations"])
        self.assertIn("intake", inventory.render(self.report()))

    def test_acked_intake_is_not_an_obligation(self):
        rid = self.receipt("old")
        intake.ack(self.con, [rid])
        old = next(r for r in self.report()["orchs"] if r["orch_id"] == "old")
        self.assertEqual(old["intake_received_count"], 0)
        self.assertFalse(old["has_obligations"])


if __name__ == "__main__":
    unittest.main()
