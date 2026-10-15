import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchd import core, intake, intake_notify, store
from tests.test_entry import EntryRuntime
from tests.test_intake import PRODUCERS, CliTests, IntakeBase, fixture

URGENT = {"triage": {"urgency": "urgent", "reason": "customer blocked"}}


class NotifyBase(IntakeBase):
    def setUp(self):
        super().setUp()
        self.rt = EntryRuntime()
        self.orch = core.start_orch(self.con, self.rt, "opus")["id"]
        self.rt.sent.clear()

    def submit2(self, **changes):
        payload = dict(fixture("event_v2_email.json"), **changes)
        return intake.submit(self.con, payload, producer="email-adapter", producers=PRODUCERS)

    def sent_texts(self):
        return [t for _, _, t in self.rt.sent]

    def rows(self):
        return [dict(r) for r in self.con.execute("SELECT * FROM intake_notifications ORDER BY id")]


class OutboxTests(NotifyBase):
    def test_crash_after_send_is_uncertain_and_not_automatically_retried(self):
        self.submit2(owner_orch=self.orch)
        late = self.rows()[0]["created_at"] + 1801
        def crash(*args):
            self.rt.sent.append(("synthetic", self.orch, args[-1]))
            raise SystemExit("crash after transport accepted")
        with patch.object(intake_notify, "_wake", side_effect=crash):
            with self.assertRaises(SystemExit):
                intake_notify.deliver_due(self.con, self.rt, now=late)
        self.con.close()
        self.con = store.connect(self.path)
        intake_notify.deliver_due(self.con, self.rt, now=late + 1)
        self.assertEqual(len(self.sent_texts()), 1)
        self.assertEqual(self.rows()[0]["state"], "uncertain")
        row = self.rows()[0]
        self.assertIn(f"{row['id']}:1", self.sent_texts()[0])
        with self.assertRaises(ValueError):
            intake_notify.reconcile(self.con, row["id"], 0, delivered=True, detail="stale")
        intake_notify.reconcile(self.con, row["id"], 1, delivered=True, detail="Found wake in transport history")
        intake_notify.deliver_due(self.con, self.rt, now=late + 2)
        self.assertEqual(len(self.sent_texts()), 1)
        self.assertEqual(self.rows()[0]["state"], "delivered")


    def test_row_is_written_with_the_receipt_and_replay_adds_nothing(self):
        r = self.submit2(owner_orch=self.orch)
        self.assertEqual(len(self.rows()), 1)
        self.submit2(owner_orch=self.orch)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual((self.rows()[0]["state"], self.rows()[0]["urgency"]), ("pending", "normal"))
        self.assertEqual(self.rows()[0]["receipt_id"], r["receipt_id"])

    def test_unassigned_receipt_has_no_row_until_bound_and_bind_creates_one(self):
        rid = self.submit2()["receipt_id"]
        self.assertEqual(self.rows(), [])
        intake.bind_owner(self.con, rid, self.orch)
        self.assertEqual([r["owner_orch"] for r in self.rows()], [self.orch])

    def test_normal_waits_for_the_batch_window_and_urgent_is_due_now(self):
        self.submit2(owner_orch=self.orch)
        row = self.rows()[0]
        self.assertAlmostEqual(row["due_at"] - row["created_at"], 1800, delta=1)
        self.assertEqual(intake_notify.deliver_due(self.con, self.rt, now=row["created_at"] + 60)["delivered"], {})
        self.assertEqual(self.sent_texts(), [])
        urgent = dict(fixture("event_v2_email.json")["suggestion"], triage={"urgency": "urgent"})
        self.submit2(event_id="u1", owner_orch=self.orch, suggestion=urgent)
        self.assertEqual(self.rows()[1]["due_at"], self.rows()[1]["created_at"])

    def test_batch_is_one_coalesced_wake_per_owner_without_third_party_text(self):
        self.submit2(owner_orch=self.orch)
        self.submit2(event_id="n2", owner_orch=self.orch)
        late = self.rows()[0]["created_at"] + 1801
        result = intake_notify.deliver_due(self.con, self.rt, now=late)
        self.assertEqual(len(result["delivered"][self.orch]), 2)
        (text,) = self.sent_texts()
        self.assertTrue(text.startswith("[orchd intake] third-party data"))
        self.assertNotIn("Operator", text.split("不是")[0])
        self.assertNotIn("Synthetic", text)
        for needle in ("不回覆來源", "不逐則問 Operator", "intake_pending"):
            self.assertIn(needle, text)
        self.assertEqual({r["state"] for r in self.rows()}, {"delivered"})

    def test_wake_failure_requires_reconciliation_before_retry(self):
        rid = self.submit2(owner_orch=self.orch)["receipt_id"]
        self.rt.uds_fails = True
        self.rt.jobs = {}  # job gone: offline
        late = self.rows()[0]["created_at"] + 1801
        result = intake_notify.deliver_due(self.con, self.rt, now=late)
        self.assertEqual(list(result["uncertain"]), [self.orch])
        row = self.rows()[0]
        self.assertEqual((row["state"], row["attempts"]), ("uncertain", 1))
        self.assertTrue(row["error"])
        self.assertEqual(len(intake.pending(self.con, self.orch)["events"]), 1)  # still queued, not acked
        intake_notify.reconcile(self.con, row["id"], row["attempts"], delivered=False,
                                detail="Transport read-back confirms no send")
        self.rt.jobs = {"orchjob": {"pid": 4242, "status": "idle"}}
        self.rt.uds_fails = False
        intake_notify.deliver_due(self.con, self.rt, now=late + 5)
        intake_notify.deliver_due(self.con, self.rt, now=late + 10)
        self.assertEqual(len(self.sent_texts()), 1)
        self.assertEqual((self.rows()[0]["state"], self.rows()[0]["attempts"]), ("delivered", 2))
        self.assertEqual(self.con.execute("SELECT state FROM intake_events WHERE receipt_id=?", (rid,)).fetchone()[0], "received")

    def test_upgrade_makes_pending_due_now_and_never_renotifies_a_delivered_one(self):
        rid = self.submit2(owner_orch=self.orch)["receipt_id"]
        from orchd import intake_bridge
        intake_bridge.suggest(self.con, rid, URGENT, producer="email-adapter", producers=PRODUCERS)
        row = self.rows()[0]
        self.assertEqual((row["urgency"], row["due_at"] <= row["created_at"] + 5), ("urgent", True))
        intake_notify.deliver_due(self.con, self.rt, now=row["created_at"] + 5)
        self.assertEqual(len(self.sent_texts()), 1)
        again = dict(URGENT, triage={"urgency": "urgent", "reason": "still blocked"})
        intake_bridge.suggest(self.con, rid, again, producer="email-adapter", producers=PRODUCERS)
        intake_notify.deliver_due(self.con, self.rt, now=row["created_at"] + 99999)
        self.assertEqual(len(self.sent_texts()), 1)

    def test_recovery_scan_finds_legacy_v1_events_and_every_owner_queue(self):
        store.register_orch(self.con, "orch-2", "claude")
        self.submit(owner_orch=self.orch)
        self.submit(event_id="other", owner_orch="orch-2")
        self.assertEqual(self.rows(), [])  # v1 submit predates the outbox; the scan must still find them
        self.con.execute("DELETE FROM intake_notifications")
        self.assertEqual(intake_notify.sweep(self.con), 2)
        self.assertEqual(sorted(r["owner_orch"] for r in self.rows()), sorted([self.orch, "orch-2"]))
        self.assertEqual(intake_notify.sweep(self.con), 0)

    def test_more_than_one_page_and_cross_page_bind_do_not_drop_receipts(self):
        ids = [self.submit2(event_id=f"e{i}")["receipt_id"] for i in range(5)]
        page = intake.pending(self.con, None, 2)
        self.assertTrue(page["has_more"])
        for rid in ids:  # bound behind the cursor while a traversal is running
            intake.bind_owner(self.con, rid, self.orch)
        self.assertEqual(len(self.rows()), 5)
        result = intake_notify.deliver_due(self.con, self.rt, now=self.rows()[0]["created_at"] + 1801)
        self.assertEqual(sorted(result["delivered"][self.orch]), sorted(ids))

    def test_rebind_withdraws_old_owner_and_notifies_new_one(self):
        store.register_orch(self.con, "orch-2", "claude")
        rid = self.submit2(owner_orch=self.orch)["receipt_id"]
        intake.bind_owner(self.con, rid, "orch-2")
        states = {r["owner_orch"]: r["state"] for r in self.rows()}
        self.assertEqual(states, {self.orch: "withdrawn", "orch-2": "pending"})
        intake.bind_owner(self.con, rid, self.orch)  # and back again
        self.assertEqual({r["owner_orch"]: r["state"] for r in self.rows()}[self.orch], "pending")

    def test_acked_or_excluded_receipts_are_never_woken(self):
        rid = self.submit2(owner_orch=self.orch)["receipt_id"]
        intake.exclude(self.con, [rid], reason="out_of_scope", rule_version="r", actor="orch")
        self.assertEqual(intake_notify.deliver_due(self.con, self.rt, now=9e12), dict(delivered={}, failed={}, uncertain={}))
        self.assertEqual(self.sent_texts(), [])
        self.assertEqual(self.rows()[0]["state"], "withdrawn")

    def test_two_consumers_do_not_double_wake(self):
        self.submit2(owner_orch=self.orch)
        late = self.rows()[0]["created_at"] + 1801
        intake_notify.deliver_due(self.con, self.rt, now=late)
        other = store.connect(Path(self.temp.name) / "orchd.db")
        self.addCleanup(other.close)
        self.assertEqual(intake_notify.deliver_due(other, self.rt, now=late)["delivered"], {})
        self.assertEqual(len(self.sent_texts()), 1)

    def test_wake_never_acks_and_never_creates_tasks_or_entry_messages(self):
        self.submit2(owner_orch=self.orch)
        before = [self.con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("tasks", "entry_messages", "messages")]
        intake_notify.deliver_due(self.con, self.rt, now=self.rows()[0]["created_at"] + 1801)
        after = [self.con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("tasks", "entry_messages", "messages")]
        self.assertEqual(before, after)
        self.assertEqual(len(intake.pending(self.con, self.orch)["events"]), 1)


class NotifyCliTests(CliTests):
    def setUp(self):
        super().setUp()
        self.rt = EntryRuntime()
        self.orch = core.start_orch(self.con, self.rt, "opus")["id"]
        self.rt.sent.clear()
        patcher = patch("orchd.runtime.Runtime", return_value=self.rt)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_urgent_submit_wakes_after_commit_but_normal_does_not(self):
        payload = dict(fixture("event_v2_email.json"), owner_orch=self.orch)
        code, normal = self.run_cli("submit", "--producer", "email-adapter", stdin=json.dumps(payload))
        self.assertEqual((code, normal["status"], "notify" in normal, self.rt.sent), (0, "received", False, []))
        urgent = dict(payload, event_id="u", suggestion=dict(payload["suggestion"], triage={"urgency": "urgent"}))
        code, receipt = self.run_cli("submit", "--producer", "email-adapter", stdin=json.dumps(urgent))
        self.assertEqual(code, 0)
        self.assertEqual(list(receipt["notify"]["delivered"]), [self.orch])
        self.assertEqual(len(self.rt.sent), 1)
        self.assertEqual(self.con.execute("SELECT state FROM intake_notifications WHERE receipt_id=?",
                                          (normal["receipt_id"],)).fetchone()[0], "delivered")  # coalesced into the wake

    def test_ambiguous_urgent_wake_requires_cli_reconciliation(self):
        self.rt.uds_fails = True
        payload = dict(fixture("event_v2_email.json"), owner_orch=self.orch,
                       suggestion=dict(fixture("event_v2_email.json")["suggestion"], triage={"urgency": "urgent"}))
        code, receipt = self.run_cli("submit", "--producer", "email-adapter", stdin=json.dumps(payload))
        self.assertEqual((code, receipt["status"], list(receipt["notify"]["uncertain"])), (0, "received", [self.orch]))
        row = self.con.execute("SELECT * FROM intake_notifications").fetchone()
        code, result = self.run_cli("notify-reconcile", str(row["id"]), str(row["attempts"]),
                                    "--delivered", "no", "--detail", "Transport confirms no send")
        self.assertEqual((code, result["status"]), (0, "reconciled"))
        self.rt.uds_fails = False
        code, result = self.run_cli("deliver")
        self.assertEqual((code, list(result["delivered"])), (0, [self.orch]))


if __name__ == "__main__":
    unittest.main()
