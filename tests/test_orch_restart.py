import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from orchd import cli, core, entry, orch_restart, store
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
