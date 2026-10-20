import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from orchd import cli, core, entry, interface, orch_restart, store
from orchd.runtime import ORCH_MODELS
from tests.test_orchd import FakeRuntime


class RestartTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "orchd.db"
        self.con = store.connect(self.path)
        self.addCleanup(self.con.close)
        self.rt = FakeRuntime()
        self.rt.jobs = {"oldjob": dict(pid=4242, status="busy")}
        store.register_orch(self.con, "old", "claude", model=ORCH_MODELS["sonnet"],
                            socket="/fake/old", session_id="old-session", job_id="oldjob")
        self.con.execute("INSERT INTO entries(id,orch_id,bound_at) VALUES('desktop','old',0)")
        self.rt.stop_worker = Mock(side_effect=self.stop)
        self.rt.start_orch = Mock(side_effect=self.start)

    def stop(self, job):
        self.rt.jobs.pop(job, None)
        self.rt.dead_sockets = {"/fake/old"}

    def start(self, orch_id, model, home):
        self.rt.jobs["newjob"] = dict(pid=4243, status="idle")
        return "/fake/new", "newjob", "new-session"

    def task(self):
        return core.dispatch(self.con, self.rt, orch_thread="old", repo="demo", title="T",
                             instructions="x", done_when="y", model="sonnet", model_reason="r",
                             task_type="code")["id"]

    def run_restart(self, **kwargs):
        return orch_restart.restart(self.con, self.rt, **kwargs)

    def assert_no_start(self, result, step):
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["step"], step)
        self.assertIn("manual_recovery", result)
        self.rt.start_orch.assert_not_called()

    def test_normal_sequence_adopts_real_task_and_reads_back_binding(self):
        tid = self.task()
        worker = dict(store.get_task(self.con, tid))
        out = self.run_restart()
        self.assertEqual(out["status"], "done", out)
        self.assertEqual(out["model"], "sonnet")
        self.assertEqual(out["adopt"]["adopted"], [tid])
        self.assertTrue(out["adopt"]["committed"])
        self.assertEqual(out["adopt"]["notification_errors"], {})
        self.assertEqual(store.get_task(self.con, tid)["orch_thread"], out["new_orch"])
        for key in ("job_id", "worktree", "branch", "session_id"):
            self.assertEqual(store.get_task(self.con, tid)[key], worker[key])
        self.assertEqual(out["binding_status"]["orch_id"], out["new_orch"])
        self.rt.stop_worker.assert_called_once_with("oldjob")
        self.assertEqual([s["step"] for s in out["steps"]],
                         ["stop", "confirm-dead", "start", "adopt", "binding"])
        self.assertEqual(out["first_prompt"], "盤點上次")

    def test_no_tasks_skips_adopt_and_model_override(self):
        with patch.object(core, "adopt") as adopt:
            out = self.run_restart(model="opus")
        self.assertEqual(out["status"], "done", out)
        self.assertTrue(out["adopt"]["skipped"])
        adopt.assert_not_called()
        self.assertEqual(self.rt.start_orch.call_args.args[1], ORCH_MODELS["opus"])

    def test_failed_stop_output_is_not_death_evidence(self):
        self.rt.stop_worker.side_effect = None  # stop silently fails, as check=False can do
        out = self.run_restart()
        self.assert_no_start(out, "confirm-dead")
        self.assertEqual(out["old_health"]["state"], "alive")
        self.assertIsNotNone(store.get_orch(self.con, "old")["stopped_at"])

    def test_runtime_unknown_never_starts(self):
        self.rt.live_jobs = Mock(return_value=None)
        self.assert_no_start(self.run_restart(), "confirm-dead")

    def test_runtime_probe_failure_never_starts(self):
        self.rt.live_jobs = Mock(side_effect=OSError("probe failed"))
        self.assert_no_start(self.run_restart(), "confirm-dead")

    def test_dead_job_with_live_or_unknown_socket_never_starts(self):
        for listening in (True, None):
            with self.subTest(listening=listening):
                self.rt.socket_listening = Mock(return_value=listening)
                self.assert_no_start(self.run_restart(), "confirm-dead")

    def test_missing_binding_lists_candidates_and_does_not_guess(self):
        self.con.execute("DELETE FROM entries")
        out = self.run_restart()
        self.assert_no_start(out, "select")
        self.assertEqual(out["candidates"]["orchs"][0]["orch_id"], "old")
        self.rt.stop_worker.assert_not_called()

    def test_stale_binding_lists_candidates(self):
        self.con.execute("UPDATE entries SET orch_id='missing'")
        out = self.run_restart()
        self.assert_no_start(out, "select")
        self.assertIn("candidates", out)

    def test_explicit_unbound_old_leaves_binding_alone(self):
        self.con.execute("DELETE FROM entries")
        out = self.run_restart(old_id="old")
        self.assertEqual(out["status"], "done", out)
        self.assertNotIn("binding", out)
        self.assertEqual(self.con.execute("SELECT count(*) FROM entries").fetchone()[0], 0)

    def test_partial_adopt_notification_failure_stops_after_commit(self):
        tid = self.task()
        self.rt.send_uds = Mock(side_effect=OSError("socket failed"))
        out = self.run_restart()
        self.assertEqual(out["step"], "adopt")
        self.assertEqual(out["status"], "failed")
        self.assertTrue(out["adopt"]["committed"])
        self.assertIn("new_owner_error", out["adopt"])
        self.assertEqual(store.get_task(self.con, tid)["orch_thread"], out["new_orch"])
        self.assertEqual(entry.get_entry(self.con, "desktop")["orch_id"], "old")
        self.assertIn("already committed", out["manual_recovery"])

    def test_adopt_uncommitted_or_notification_errors_stop_before_binding(self):
        self.task()
        for adoption in (dict(committed=False), dict(committed=True, notification_errors={"old": "failed"})):
            with self.subTest(adoption=adoption), patch.object(core, "adopt", return_value=adoption):
                out = self.run_restart()
                self.assertEqual(out["step"], "adopt")
                self.assertEqual(out["status"], "failed")
                self.assertEqual(entry.get_entry(self.con, "desktop")["orch_id"], "old")

    def test_dry_run_does_not_mutate_any_registry_or_runtime_state(self):
        self.task()
        before = list(self.con.iterdump())
        out = self.run_restart(dry_run=True)
        self.assertEqual(out["status"], "dry-run")
        self.assertIn("orchd orch start --model sonnet --no-attach", out["plan"])
        self.assertEqual(list(self.con.iterdump()), before)
        self.rt.stop_worker.assert_not_called()
        self.rt.start_orch.assert_not_called()

    def test_cli_dry_run_uses_private_snapshot_and_reports_plan(self):
        before = list(self.con.iterdump())
        with patch.dict(os.environ, ORCHD_HOME=self.tmp.name), patch.object(cli, "Runtime", return_value=self.rt), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(["orch", "restart", "--dry-run"]), 0)
        self.assertIn('"status": "dry-run"', output.getvalue())
        self.assertEqual(list(self.con.iterdump()), before)
        self.rt.stop_worker.assert_not_called()
        self.rt.start_orch.assert_not_called()

    def test_cli_success_prints_new_id_and_prompt_on_last_line(self):
        with patch.dict(os.environ, ORCHD_HOME=self.tmp.name), patch.object(cli, "Runtime", return_value=self.rt), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(["orch", "restart"]), 0)
        new_id = entry.get_entry(self.con, "desktop")["orch_id"]
        self.assertEqual(output.getvalue().splitlines()[-1], f"New Orch: {new_id}; first prompt: 盤點上次")

    def test_cli_failed_confirmation_returns_nonzero(self):
        self.rt.stop_worker.side_effect = None
        with patch.dict(os.environ, ORCHD_HOME=self.tmp.name), patch.object(cli, "Runtime", return_value=self.rt), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(["orch", "restart"]), 1)
        self.assertIn('"step": "confirm-dead"', output.getvalue())
        self.rt.start_orch.assert_not_called()

    def test_stop_and_start_failures_have_exact_stage_and_recovery(self):
        self.rt.stop_worker.side_effect = OSError("stop failed")
        self.assert_no_start(self.run_restart(), "stop")
        self.rt.stop_worker.side_effect = self.stop
        self.rt.start_orch.side_effect = OSError("start failed")
        out = self.run_restart()
        self.assertEqual(out["step"], "start")
        self.assertEqual(out["status"], "failed")
        self.assertIn("orchd orch start --model sonnet --no-attach", out["manual_recovery"])

    def test_binding_failure_and_readback_failure_are_reported(self):
        with patch.object(entry, "bind", side_effect=OSError("bind failed")):
            out = self.run_restart()
        self.assertEqual(out["step"], "binding")
        self.assertIsNotNone(out["new_orch"])
        with patch.object(entry, "snapshot", side_effect=OSError("readback failed")):
            out = self.run_restart()
        self.assertEqual(out["step"], "binding-status")
        self.assertEqual(out["status"], "failed")

    def test_binding_notice_failure_preserves_committed_binding(self):
        self.rt.send_uds = Mock(side_effect=OSError("notice failed"))
        out = self.run_restart()
        self.assertEqual(out["step"], "binding-status")
        self.assertEqual(out["binding_status"]["orch_id"], out["new_orch"])
        self.assertEqual(out["status"], "failed")

    def test_changed_binding_is_not_overwritten(self):
        original = self.start

        def change_binding(*args):
            self.con.execute("UPDATE entries SET orch_id='other'")
            return original(*args)

        self.rt.start_orch.side_effect = change_binding
        out = self.run_restart()
        self.assertEqual(out["step"], "binding")
        self.assertEqual(out["status"], "failed")
        self.assertEqual(entry.get_entry(self.con, "desktop")["orch_id"], "other")

    def adapter(self, name="adapter"):
        registration = interface.register(self.con, name=name, orch_id="old")
        return dict(name=name, token=registration["token"])

    def submit(self, auth, key="one", con=None):
        return interface.submit(con or self.con, self.rt, **auth, conversation="thread",
                                request_key=key, text="question")

    def test_restart_moves_all_interfaces_preserving_tokens_and_inactive_leases(self):
        active = self.adapter()
        disabled = self.adapter("disabled")
        self.adapter("expired")
        interface.deregister(self.con, **disabled)
        self.con.execute("UPDATE interfaces SET expires_at=0 WHERE name='expired'")
        before = [dict(row) for row in self.con.execute("SELECT * FROM interfaces ORDER BY name")]
        out = self.run_restart()
        self.assertEqual(out["status"], "done", out)
        self.assertEqual(out["interfaces_migrated"], 3)
        after = [dict(row) for row in self.con.execute("SELECT * FROM interfaces ORDER BY name")]
        self.assertEqual(after, [dict(row, orch_id=out["new_orch"]) for row in before])
        interface.register(self.con, **active, orch_id=out["new_orch"])
        interface.register(self.con, name="new-adapter", orch_id=out["new_orch"])
        with self.assertRaises(ValueError):
            interface.register(self.con, name="wrong-owner", orch_id="old")
        with self.assertRaisesRegex(ValueError, "inactive"):
            self.submit(disabled)
        with patch.object(core, "_notify_orch") as notify:
            self.assertIsNone(self.submit(active)["wake_error"])
            self.assertEqual(notify.call_args.args[2], out["new_orch"])

    def test_restart_preserves_pending_replies_and_immutable_origin_routes(self):
        auth = self.adapter()
        rid = self.submit(auth)["request_id"]
        interface.reply(self.con, "old", request_id=rid, text="answer")
        before = dict(self.con.execute("SELECT * FROM interface_requests WHERE id=?", (rid,)).fetchone())
        out = self.run_restart()
        self.assertEqual(out["status"], "done", out)
        after = dict(self.con.execute("SELECT * FROM interface_requests WHERE id=?", (rid,)).fetchone())
        self.assertEqual(after, dict(before, orch_id=out["new_orch"]))
        self.assertEqual(interface.fetch(self.con, **auth, conversation="thread"),
                         [dict(request_id=rid, conversation="thread", reply="answer")])
        self.assertEqual(self.submit(auth)["request_id"], rid)
        for _ in range(2):
            interface.ack(self.con, **auth, conversation="thread", request_id=rid)
        self.assertEqual(interface.fetch(self.con, **auth, conversation="thread"), [])

    def test_restart_carries_pending_interim_and_accepts_final_from_new_owner(self):
        auth = self.adapter()
        rid = self.submit(auth)["request_id"]
        interface.reply(self.con, "old", request_id=rid, text="working", kind="interim")
        out = self.run_restart()
        self.assertEqual(out["status"], "done", out)
        both = ["interim", "final"]
        self.assertEqual(interface.fetch(self.con, **auth, conversation="thread", kinds=both),
                         [dict(request_id=rid, conversation="thread", kind="interim", reply="working")])
        self.assertEqual([r["id"] for r in interface.requests(self.con, out["new_orch"])], [rid])
        interface.reply(self.con, out["new_orch"], request_id=rid, text="done")
        self.assertEqual([r["kind"] for r in interface.fetch(self.con, **auth, conversation="thread", kinds=both)], both)

    def test_restart_rewakes_unanswered_requests_including_previously_delivered(self):
        auth = self.adapter()
        delivered = self.submit(auth)["request_id"]
        with patch.object(interface, "wake", return_value="OSError"):
            pending = self.submit(auth, "pending")["request_id"]
        with patch.object(core, "_notify_orch") as notify:
            out = self.run_restart()
        self.assertEqual(out["status"], "done", out)
        self.assertEqual(out["interface_wakes"], {delivered: None, pending: None})
        self.assertEqual([call.args[2] for call in notify.call_args_list], [out["new_orch"]] * 2)
        self.assertEqual([r["id"] for r in interface.requests(self.con, out["new_orch"])], [delivered, pending])
        interface.reply(self.con, out["new_orch"], request_id=pending, text="recovered")
        self.assertEqual(interface.fetch(self.con, **auth, conversation="thread")[0]["request_id"], pending)

    def test_interface_move_rolls_back_with_binding(self):
        self.adapter()
        original = interface.migrate_owner

        def fail(*args):
            original(*args)
            raise OSError("migration failed")

        with patch.object(interface, "migrate_owner", side_effect=fail):
            out = self.run_restart()
        self.assertEqual(out["status"], "failed")
        self.assertEqual(entry.get_entry(self.con, "desktop")["orch_id"], "old")
        self.assertEqual(self.con.execute("SELECT orch_id FROM interfaces").fetchone()[0], "old")

    def test_restart_unbound_orch_still_migrates_interfaces(self):
        auth = self.adapter()
        self.con.execute("DELETE FROM entries")
        out = self.run_restart(old_id="old")
        self.assertEqual(out["status"], "done", out)
        self.assertEqual(out["interfaces_migrated"], 1)
        self.assertIsNone(self.submit(auth)["wake_error"])

    def test_restart_serializes_with_submit_committed_before_wake(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event
        auth = self.adapter()
        committed, resume = Event(), Event()
        original = interface.wake

        def pause(con, rt, rid):
            committed.set()
            if not resume.wait(10):
                raise TimeoutError("restart did not finish")
            return original(con, rt, rid)

        def send():
            con = store.connect(self.path)
            try:
                with patch.object(interface, "wake", side_effect=pause):
                    return self.submit(auth, con=con)
            finally:
                con.close()

        # Restart's sweep must use the real wake rather than the paused submit.
        original_sweep = interface.sweep
        def sweep(con, rt):
            with patch.object(interface, "wake", original):
                return original_sweep(con, rt)

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(send)
            try:
                self.assertTrue(committed.wait(10))
                with patch.object(interface, "sweep", side_effect=sweep):
                    out = self.run_restart()
                self.assertEqual(out["status"], "done", out)
            finally:
                resume.set()
            receipt = future.result(timeout=10)
        self.assertIsNone(receipt["wake_error"])
        row = self.con.execute("SELECT * FROM interface_requests WHERE id=?", (receipt["request_id"],)).fetchone()
        self.assertEqual(row["orch_id"], out["new_orch"])
        self.assertIsNotNone(row["wake_at"])

    def test_submit_during_migration_waits_and_uses_new_owner(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event
        auth = self.adapter()
        ready, submit_now, started = Event(), Event(), Event()
        original = interface.migrate_owner

        def send():
            con = store.connect(self.path)
            try:
                ready.set()
                if not submit_now.wait(10):
                    raise TimeoutError("migration did not start")
                started.set()
                return self.submit(auth, con=con)
            finally:
                con.close()

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(send)
            try:
                self.assertTrue(ready.wait(10))
                def migrate(*args):
                    submit_now.set()
                    self.assertTrue(started.wait(10))
                    return original(*args)
                with patch.object(interface, "migrate_owner", side_effect=migrate):
                    out = self.run_restart()
                self.assertEqual(out["status"], "done", out)
            finally:
                submit_now.set()
            receipt = future.result(timeout=10)
        self.assertIsNone(receipt["wake_error"])
        self.assertEqual(self.con.execute("SELECT orch_id FROM interface_requests WHERE id=?",
                                         (receipt["request_id"],)).fetchone()[0], out["new_orch"])
