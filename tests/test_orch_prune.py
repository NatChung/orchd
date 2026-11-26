from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import Mock

from orchd import intake, orch_prune, store
from orchd.channels import repository


class PruneTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name).resolve() / "orchd.db"
        self.manifest = self.path.with_name("manifest.toml")
        self.manifest.write_text("slack_configs=[]\nline_configs=[]\n", encoding="utf-8")
        self.con = repository.connect(self.path)
        self.addCleanup(self.con.close)
        self.rt = Mock()
        self.jobs = {}
        self.rt.live_jobs.side_effect = lambda **kw: self.jobs.copy()
        self.rt.socket_listening.side_effect = lambda path: path.removeprefix("/") in self.jobs

        def stop(job):
            self.assertFalse(self.con.in_transaction)
            self.assertIsNotNone(store.get_orch(self.con, job)["prune_requested_at"])
            del self.jobs[job]

        self.rt.stop_worker.side_effect = stop

    def owner(self, name, status="idle"):
        store.register_orch(self.con, name, "claude", socket="/" + name, session_id=name, job_id=name)
        self.con.execute("UPDATE orchs SET created_at=1 WHERE id=?", (name,))
        self.jobs[name] = {"pid": len(self.jobs) + 1, "sessionId": name, "status": status}

    def test_stop_only_unbound_idle_owner_and_preserve_registry(self):
        for name in ("desktop-owner", "channel-owner", "listener-owner", "unbound"):
            self.owner(name)
        self.owner("busy", "busy")
        self.owner("pending")
        self.con.execute("INSERT INTO entries(id,orch_id,bound_at) VALUES('desktop','desktop-owner',1)")
        self.con.executemany(
            "INSERT INTO entry_messages(entry_id,orch_id,direction,kind,body,body_bytes,body_sha256,"
            "question_state,delivery,created_at) VALUES('desktop',?,'out','question','q',1,'hash',?,'delivered',1)",
            [("unbound", "answered"), ("pending", "snoozed")],
        )
        self.con.execute(
            "INSERT INTO interfaces(id,name,orch_id,token_hash,expires_at,idle_seconds,disabled) "
            "VALUES('listener','synthetic-listener','listener-owner','hash',1,5,0)"
        )
        self.con.execute(
            "INSERT INTO channel_sources(id,host_id,provider,account_ref,adapter_id,enabled,manifest_hash,created_at) "
            "VALUES('source','host','line','account','line',0,'hash','now')"
        )
        self.con.execute(
            "INSERT INTO channel_routes(id,version,scope_key,source_id,conversation_ref,owner_orch,mode,"
            "authority_epoch,effective_at,manifest_hash) "
            "VALUES('route',1,'scope','source','conversation','channel-owner','receive_only',1,'now','hash')"
        )
        rows = {row["orch_id"]: row for row in orch_prune.run(self.con, self.rt)}
        self.assertEqual(rows["unbound"]["status"], "eligible")
        self.rt.stop_worker.assert_not_called()
        with self.assertRaisesRegex(ValueError, "cleanup apply requires --routing-config"):
            orch_prune.run(self.con, self.rt, apply=True)
        with self.assertRaisesRegex(ValueError, "cleanup apply requires --routing-config"):
            orch_prune.run(self.con, self.rt, "unbound", apply=True)
        for name in ("desktop-owner", "channel-owner", "listener-owner", "busy", "pending"):
            result, = orch_prune.run(self.con, self.rt, name, apply=True, routing_config=self.manifest)
            self.assertEqual(result["status"], "retained", result)
        result, = orch_prune.run(self.con, self.rt, "unbound", apply=True, routing_config=self.manifest)
        self.assertEqual(result["status"], "stopped_and_archived")
        self.rt.stop_worker.assert_called_once_with("unbound")
        record = store.get_orch(self.con, "unbound")
        self.assertIsNotNone(record["archived_at"])
        self.assertIsNone(record["prune_requested_at"])

    def test_unverified_stop_can_be_reconciled_without_second_stop(self):
        self.owner("unbound")
        self.rt.stop_worker.side_effect = None
        result, = orch_prune.run(self.con, self.rt, "unbound", apply=True, routing_config=self.manifest)
        self.assertEqual(result["status"], "unconfirmed")
        row = store.get_orch(self.con, "unbound")
        self.assertIsNone(row["archived_at"])
        self.assertIsNotNone(row["prune_requested_at"])
        del self.jobs["unbound"]
        result, = orch_prune.run(self.con, self.rt, "unbound", apply=True, routing_config=self.manifest)
        self.assertEqual(result["status"], "stopped_and_archived")
        self.rt.stop_worker.assert_called_once()

    def test_batch_selects_only_unbound_owners_and_rechecks_after_restart(self):
        for name in ("desktop", "slack", "line", "cached-line", "worker", "orphan", "late-binding"):
            self.owner(name)
        self.owner("fresh")
        self.con.execute("UPDATE orchs SET created_at=strftime('%s','now') WHERE id='fresh'")
        self.con.execute("INSERT INTO entries(id,orch_id,bound_at) VALUES('desktop','desktop',1)")
        self.con.execute(
            "INSERT INTO tasks(id,repo,repo_path,title,instructions,done_when,orch_thread,codex_bin,status,"
            "created_at,updated_at) VALUES('task','demo','/example','title','instructions','done','worker','codex',"
            "'running',1,1)"
        )
        slack = self.path.with_name("slack.toml")
        line = self.path.with_name("line.toml")
        manifest = self.path.with_name("prune.toml")
        slack.write_text('[[accounts]]\norchd_orch_id="slack"\n', encoding="utf-8")
        line.write_text('[runner.owners]\n' 'example="line"\n' 'default="@bound"\n', encoding="utf-8")
        manifest.write_text(
            f"slack_configs=[{json.dumps(str(slack))}]\nline_configs=[{json.dumps(str(line))}]\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(ValueError, "resolve running LINE"):
            orch_prune.run(self.con, self.rt, apply=True, routing_config=manifest)
        self.rt.stop_worker.assert_not_called()
        with manifest.open("a", encoding="utf-8") as stream:
            stream.write('line_bound_owners=["cached-line"]\n')
        single, = orch_prune.run(self.con, self.rt, "slack", apply=True, routing_config=manifest)
        self.assertEqual(single["reasons"], ["external_listener_binding"])
        self.rt.stop_worker.assert_not_called()

        # A binding added between the batch preview and a stop must win.
        original = self.rt.live_jobs.side_effect
        calls = 0

        def refresh_jobs(**kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                self.con.execute("INSERT INTO entries(id,orch_id,bound_at) VALUES('new','late-binding',1)")
            return original(**kwargs)

        self.rt.live_jobs.side_effect = refresh_jobs
        pruned = orch_prune.run(self.con, self.rt, apply=True, routing_config=manifest)
        self.assertEqual([r["orch_id"] for r in pruned if r["status"] == "stopped_and_archived"], ["orphan"])
        self.assertEqual(set(self.jobs), {"desktop", "slack", "line", "cached-line", "worker", "late-binding", "fresh"})
        self.rt.stop_worker.assert_called_once_with("orphan")
        orch_prune.run(self.con, self.rt, apply=True, routing_config=manifest)
        self.rt.stop_worker.assert_called_once()

    def test_overlapping_batch_skips_without_contacting_runtime(self):
        manifest = self.path.with_name("prune.toml")
        manifest.write_text("slack_configs=[]\nline_configs=[]\n", encoding="utf-8")
        with store.task_delivery(self.con, ["orch-prune-batch"]):
            rows = orch_prune.run(self.con, self.rt, apply=True, routing_config=manifest)
        self.assertEqual(rows[0]["reasons"], ["batch_already_running"])
        self.rt.live_jobs.assert_not_called()


    def test_provider_becoming_busy_after_reservation_releases_new_stop_intent(self):
        self.owner("orphan")
        original = self.rt.socket_listening.side_effect
        calls = 0

        def socket_probe(path):
            nonlocal calls
            calls += 1
            if calls == 1:
                self.jobs["orphan"] = {**self.jobs["orphan"], "status": "busy"}
            return original(path)

        self.rt.socket_listening.side_effect = socket_probe
        result, = orch_prune.run(self.con, self.rt, "orphan", apply=True, routing_config=self.manifest)
        self.assertEqual(result["reasons"], ["orch_not_idle"])
        self.assertIsNone(store.get_orch(self.con, "orphan")["stopped_at"])
        self.rt.stop_worker.assert_not_called()


    def test_pending_worker_notification_survives_task_close(self):
        self.owner("orphan")
        self.con.execute(
            "INSERT INTO tasks(id,repo,repo_path,title,instructions,done_when,orch_thread,codex_bin,status,"
            "created_at,updated_at) VALUES('task','demo','/example','title','instructions','done','orphan','codex',"
            "'closed',1,1)"
        )
        store.add_message(self.con, "task", "report", "Pending worker report")
        result, = orch_prune.run(self.con, self.rt, "orphan", apply=True, routing_config=self.manifest)
        self.assertIn("pending_worker_notifications", result["reasons"])
        self.rt.stop_worker.assert_not_called()

    def test_intake_bind_cannot_cross_a_reserved_stop(self):
        self.owner("orphan")
        self.con.execute(
            "INSERT INTO intake_events(receipt_id,version,provider,account,company,event_id,source_ref,"
            "source_time,received_at,producer,summary,content_sha256) "
            "VALUES('receipt',1,'line','a','org','event','group',1,1,'fixture','test','hash')"
        )
        other = store.connect(self.path)
        self.addCleanup(other.close)
        original_stop = self.rt.stop_worker.side_effect

        def stop(job):
            with self.assertRaisesRegex(intake.IntakeError, "being pruned"):
                intake.bind_owner(other, "receipt", "orphan", only_if_unassigned=True)
            original_stop(job)

        self.rt.stop_worker.side_effect = stop
        result, = orch_prune.run(self.con, self.rt, "orphan", apply=True, routing_config=self.manifest)
        self.assertEqual(result["status"], "stopped_and_archived")
        self.assertIsNone(self.con.execute("SELECT owner_orch FROM intake_events").fetchone()[0])
