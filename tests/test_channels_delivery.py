from concurrent.futures import ThreadPoolExecutor
from orchd.channels import policy
from tests.channel_outbound_helpers import OutboundCase


class DeliveryTest(OutboundCase):
    def test_three_platform_transports_share_gate_and_scoped_fake_secrets(self):
        import base64
        from email import policy as email_policy
        from email.parser import BytesParser
        from orchd.channels.adapters.base import OutboundAdapter
        from orchd.channels.adapters.slack import SlackSendTransport
        from orchd.channels.adapters.line import LineSendTransport
        from orchd.channels.adapters.email import GmailSendTransport, SEND_SCOPE, READONLY_SCOPE
        from orchd.channels.secret_store import FakeSecretStore
        ledger = []
        case = self
        class FakeClient:
            def chat_postMessage(self, **args):
                case.assertFalse(case.svc.con.in_transaction)
                ledger.append(('slack', args))
                return {'ok':True,'ts':'synthetic-ts'}
            def push_message(self, target, messages, key):
                case.assertFalse(case.svc.con.in_transaction)
                ledger.append(('line', dict(target=target,messages=messages,key=key)))
                return {'request_id':'synthetic-push'}
            def users(self): return self
            def messages(self): return self
            def send(self, **args):
                case.assertFalse(case.svc.con.in_transaction)
                ledger.append(('gmail', args))
                return self
            def execute(self, **args):
                case.assertEqual(args, {'num_retries':0})
                return {'id':'synthetic-mail'}
        for provider in ('slack','line','gmail'):
            sid = provider
            refs = {'api':'os-store:demo/api','read':'os-store:demo/read','send':'os-store:demo/send'}
            self.svc.provision_source(dict(id=sid,host_id='h',provider=provider,tenant_ref='t',account_ref='sender@example.invalid',adapter_id=sid,enabled=True,credential_refs=refs),caller=self.op)
            routes = [dict(id='r-'+sid,source_id=sid,conversation_ref='c',owner_orch=self.owner,responder_source_id=sid,mode='respond')]
            self.svc.apply_routes(routes,caller=self.op)
            adapter = policy.Caller(role='adapter', sources=frozenset({sid}), identity=sid)
            caller = policy.Caller(role='operator', sources=frozenset({sid}))
            event = self.event(provider, provider)
            event['origin']['provider'] = provider
            mid = self.svc.receive(sid,event,caller=adapter)['message_id']
            payload = dict(sender=sid,target='c',body='Synthetic exact text',expires_at=2000000000)
            if provider == 'gmail':
                payload.update(target={'to':['to@example.invalid'],'cc':['cc@example.invalid'],'bcc':['bcc@example.invalid']},thread='synthetic-thread',subject='Synthetic subject')
                directory = self.svc.channel_home/'email'
                directory.mkdir(exist_ok=True)
                (directory/'config.toml').write_text('[[sources]]\nid="gmail"\nsend_enabled=true\n')
            draft = self.svc.draft_reply(mid,payload,caller=caller)
            # The same real Entry answer flow is used for every provider.
            old_op = self.op
            self.op = caller
            approved = self.approve(draft)
            self.op = old_op
            values = {(sid,sid,refs['send' if provider=='gmail' else 'api']):b'synthetic-secret'}
            def factory(material, account):
                self.assertEqual(material,b'synthetic-secret')
                if provider=='gmail':
                    with self.assertRaises(PermissionError):
                        GmailSendTransport(FakeClient(),account,scopes=[READONLY_SCOPE])
                    return GmailSendTransport(FakeClient(),account,scopes=[SEND_SCOPE])
                return {'slack':SlackSendTransport,'line':LineSendTransport}[provider](FakeClient(),account)
            sender = OutboundAdapter(self.svc,sid,adapter,FakeSecretStore(values),factory)
            self.assertEqual(sender.sweep()[0]['state'],'provider_accepted')
            for _ in range(3): self.assertEqual(sender.sweep(),[])
            self.assertEqual(self.svc.delivery(approved['delivery_id'],caller=caller)['state'],'provider_accepted')
        self.assertEqual([p for p,_ in ledger],['slack','line','gmail'])
        mime = BytesParser(policy=email_policy.default).parsebytes(base64.urlsafe_b64decode(ledger[-1][1]['body']['raw']))
        self.assertEqual(str(mime['To']),'to@example.invalid')
        self.assertEqual(str(mime['Cc']),'cc@example.invalid')
        self.assertEqual(str(mime['Bcc']),'bcc@example.invalid')
        self.assertEqual(ledger[-1][1]['body']['threadId'],'synthetic-thread')

    def test_concurrent_claim_has_exactly_one_winner(self):
        approved = self.approve()
        factory = self.svc.session_factory()
        def claim(_):
            svc = factory()
            try:
                return svc.claim_outbound('s', caller=self.adapter)
            finally:
                svc.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            claimed = sum(list(pool.map(claim, range(2))), [])
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0]['id'], approved['delivery_id'])

    def test_gate_rechecks_pause_route_and_cross_source(self):
        self.approve()
        self.enable(paused=True)
        with self.assertRaises(PermissionError):
            self.svc.claim_outbound('s', caller=self.adapter)
        self.enable()
        with self.assertRaises(PermissionError):
            self.svc.claim_outbound('s', caller=policy.Caller(role='adapter', sources=frozenset({'other'}), identity='s'))
        self.routes(authority_epoch=2)
        self.assertEqual(self.svc.claim_outbound('s', caller=self.adapter), [])

    def test_expired_cancelled_and_disabled_source_are_never_claimed(self):
        from datetime import timedelta
        draft = self.draft()
        self.approve(draft)
        self.svc.cancel_draft(draft['draft_id'], caller=self.op)
        self.assertEqual(self.svc.claim_outbound('s',caller=self.adapter),[])
        self.approve()
        self.now += timedelta(hours=2)
        self.assertEqual(self.svc.claim_outbound('s',caller=self.adapter),[])
        self.approve()
        # Synthetic control-plane disable during the approval/claim interval.
        self.svc.con.execute("UPDATE channel_sources SET enabled=0 WHERE id='s'")
        with self.assertRaises(PermissionError):
            self.svc.claim_outbound('s',caller=self.adapter)

    def test_no_config_and_sender_reference_changes_fail_closed(self):
        self.approve()
        (self.svc.channel_home/'channels.toml').unlink()
        with self.assertRaises(PermissionError):
            self.svc.claim_outbound('s',caller=self.adapter)
        self.enable()
        with self.assertRaises(PermissionError):
            self.svc.claim_outbound('s',caller=policy.Caller(role='adapter',sources=frozenset({'s'}),identity='forged-adapter'))


    def test_interim_final_and_no_reply_have_separate_identity(self):
        interim = self.draft(reply_kind='interim')
        final = self.draft(reply_kind='final')
        self.approve(interim)
        self.approve(final)
        quiet = self.draft(reply_kind='no_reply')
        self.assertEqual(quiet['status'], 'no_reply')
        claims = self.svc.claim_outbound('s', caller=self.adapter)
        self.assertEqual({c['reply_kind'] for c in claims}, {'interim', 'final'})
        for claim in claims:
            result = self.svc.record_outcome(claim['id'], 1, 'provider_accepted', 'receipt-'+claim['id'], caller=self.adapter)
            self.assertEqual(result['state'], 'provider_accepted')
            self.assertNotIn('recipient_read', str(result))

    def test_disabled_by_default_and_receipts_are_attempt_scoped(self):
        self.approve()
        self.enable(False)
        with self.assertRaises(PermissionError):
            self.svc.claim_outbound('s', caller=self.adapter)
        self.enable()
        claim = self.svc.claim_outbound('s', caller=self.adapter)[0]
        for outcome, receipt, attempt in [('recipient_read', 'r', 1), ('provider_accepted', None, 1), ('failed', None, 2)]:
            with self.assertRaises(ValueError):
                self.svc.record_outcome(claim['id'], attempt, outcome, receipt, caller=self.adapter)
        accepted = self.svc.record_outcome(claim['id'], 1, 'provider_accepted', 'r', caller=self.adapter)
        self.assertEqual(self.svc.record_outcome(claim['id'], 1, 'provider_accepted', 'r', caller=self.adapter), accepted)
        with self.assertRaises(ValueError):
            self.svc.record_outcome(claim['id'], 1, 'failed', None, caller=self.adapter)
