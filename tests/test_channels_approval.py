import base64
import hashlib
from datetime import timedelta
from orchd import intake
from orchd.channels import policy
from tests.channel_outbound_helpers import OutboundCase


class ApprovalTest(OutboundCase):
    def test_attachment_or_snapshot_mutation_after_approval_prevents_claim(self):
        import json
        attachment = dict(name='demo.txt', bytes=3, sha256=hashlib.sha256(b'abc').hexdigest(), data_base64='YWJj')
        draft = self.draft(attachments=[attachment])
        self.approve(draft)
        # Simulate corrupt/restored storage, not a supported draft-edit API.
        row = self.svc.con.execute('SELECT payload_json FROM intake_drafts WHERE draft_id=?',(draft['draft_id'],)).fetchone()
        value = json.loads(row[0])
        value['attachments'][0]['data_base64'] = 'ZGVm'
        self.svc.con.execute('UPDATE intake_drafts SET payload_json=? WHERE draft_id=?',(json.dumps(value),draft['draft_id']))
        self.assertEqual(self.svc.claim_outbound('s',caller=self.adapter),[])

    def test_desktop_relay_approval_is_bound_to_actual_reply(self):
        from orchd import entry
        draft = self.draft()
        shown = self.svc.approval_preview(draft['draft_id'],draft['version'],caller=self.op)
        question = entry.ask_nat(self.svc.con,self.rt,self.owner,shown['preview'])['question_id']
        self.rt.nat_says('synthetic-desktop',shown['answer'])
        reply = entry.relay(self.svc.con,self.rt,'desktop','synthetic-desktop',reply_to=question)
        self.assertEqual(self.svc.approve(draft['draft_id'],draft['version'],draft['payload_hash'],reply['message_id'],caller=self.op)['status'],'approved')

    def test_exact_entry_answer_required_and_legacy_gate_cannot_bypass(self):
        draft = self.draft()
        for ref in (99999, self.answer(draft, text='can handle it')):
            with self.assertRaises((ValueError, PermissionError)):
                self.svc.approve(draft['draft_id'], draft['version'], draft['payload_hash'], ref, caller=self.op)
        approved = self.approve(draft)
        self.assertEqual(approved['status'], 'approved')
        with self.assertRaises(intake.IntakeError):
            intake.claim(self.svc.con, draft['draft_id'], draft['version'])

    def test_old_version_expired_cancelled_and_wrong_hash(self):
        old = self.draft()
        answer = self.answer(old)
        new = self.draft(draft_id=old['draft_id'], body='Changed')
        with self.assertRaises(ValueError):
            self.svc.approve(old['draft_id'], old['version'], old['payload_hash'], answer, caller=self.op)
        with self.assertRaises(ValueError):
            self.approve(new, payload_hash=old['payload_hash'])
        self.svc.cancel_draft(new['draft_id'], caller=self.op)
        with self.assertRaises(ValueError):
            self.approve(new)
        expired = self.draft()
        self.now += timedelta(hours=2)
        with self.assertRaises(ValueError):
            self.approve(expired)

    def test_attachment_bytes_digest_and_all_payload_fields_bound(self):
        attachment = dict(name='demo.txt', bytes=3, sha256=hashlib.sha256(b'abc').hexdigest(), data_base64=base64.b64encode(b'abc').decode())
        draft = self.draft(attachments=[attachment], mentions=['demo'])
        answer = self.answer(draft)
        variants = [dict(target='another'), dict(thread='thread'), dict(sender='different'),
                    dict(mentions=['other']), dict(format='markdown'), dict(account='other'), dict(source_id='other')]
        for change in variants:
            with self.subTest(change=change):
                try:
                    changed = self.draft(draft_id=draft['draft_id'], attachments=[attachment], **change)
                except (ValueError, PermissionError):
                    continue
                with self.assertRaises(ValueError):
                    self.svc.approve(changed['draft_id'], changed['version'], draft['payload_hash'], answer, caller=self.op)
        with self.assertRaises(ValueError):
            self.draft(attachments=[dict(attachment, sha256='0'*64)])

    def test_sender_grant_and_adapter_cannot_approve_or_widen_source(self):
        draft = self.draft()
        for caller in (self.worker, self.adapter, policy.Caller(role='peer', sources=frozenset({'s'}))):
            with self.assertRaises(PermissionError):
                self.svc.approve(draft['draft_id'], draft['version'], draft['payload_hash'], 1, caller=caller)
            with self.assertRaises(PermissionError):
                self.svc.provision_source({}, caller=caller)
        with self.assertRaises((ValueError, PermissionError)):
            self.draft(source_id='another')

    def test_unrelated_preview_and_rebound_entry_are_rejected(self):
        draft = self.draft()
        answer = self.answer(draft, preview='A different question')
        with self.assertRaises(ValueError):
            self.svc.approve(draft['draft_id'], draft['version'], draft['payload_hash'], answer, caller=self.op)
        answer = self.answer(draft)
        from orchd import core
        core_owner = core.start_orch(self.svc.con, self.rt, 'opus')['id']
        from orchd import entry
        entry.bind(self.svc.con, self.rt, core_owner, force=True)
        with self.assertRaises(ValueError):
            self.svc.approve(draft['draft_id'], draft['version'], draft['payload_hash'], answer, caller=self.op)
