from tests.channel_outbound_helpers import OutboundCase


class RecoveryTest(OutboundCase):
    def test_provider_acceptance_then_database_failure_recovers_without_resend(self):
        from unittest.mock import patch
        from orchd.channels.delivery import OutboundRunner
        approved = self.approve()
        class Fake:
            sends = 0
            def send(self, payload, key):
                self.sends += 1
                return 'provider-accepted-receipt'
        fake = Fake()
        runner = OutboundRunner(self.svc,'s',self.adapter,fake)
        with patch.object(self.svc,'record_outcome',side_effect=OSError('simulated lost DB receipt')):
            with self.assertRaises(OSError): runner.sweep()
        self.svc.close()
        self.open()
        self.svc.recover_outbound(caller=self.op)
        runner = OutboundRunner(self.svc,'s',self.adapter,fake)
        for _ in range(4): runner.sweep()
        self.assertEqual(fake.sends,1)
        self.assertEqual(self.svc.delivery(approved['delivery_id'],caller=self.op)['state'],'uncertain')

    def test_crash_after_claim_restart_reconcile_never_requeues(self):
        self.approve()
        claim = self.svc.claim_outbound('s', caller=self.adapter)[0]
        self.svc.close()
        self.open()
        self.assertEqual(self.svc.recover_outbound(caller=self.op)['uncertain'], 1)
        for _ in range(3):
            self.assertEqual(self.svc.claim_outbound('s', caller=self.adapter), [])
        result = self.svc.reconcile_outbound(claim['id'], 'provider_accepted', 'found-receipt', evidence='synthetic provider lookup', caller=self.op)
        self.assertEqual(result['state'], 'provider_accepted')
        history = self.svc.delivery(claim['id'], caller=self.op)['attempts']
        self.assertEqual(history[0]['state'], 'uncertain')
        self.assertEqual(history[1]['state'], 'provider_accepted')

    def test_lost_receipt_and_timeout_send_only_once_across_sweeps(self):
        from orchd.channels.delivery import OutboundRunner
        for mode in ('lost_receipt', 'timeout'):
            with self.subTest(mode=mode):
                self.approve(self.draft(body=mode))
                class FakeTransport:
                    sends = 0
                    def send(self, payload, key):
                        self.sends += 1
                        if mode == 'timeout':
                            raise TimeoutError()
                        return None  # provider accepted, receipt was lost
                transport = FakeTransport()
                runner = OutboundRunner(self.svc, 's', self.adapter, transport)
                for _ in range(4):
                    runner.sweep()
                self.assertEqual(transport.sends, 1)

    def test_adapter_cannot_reconcile_or_retry_unknown(self):
        self.approve()
        claim = self.svc.claim_outbound('s', caller=self.adapter)[0]
        self.svc.record_outcome(claim['id'], 1, 'uncertain', None, caller=self.adapter)
        with self.assertRaises(PermissionError):
            self.svc.reconcile_outbound(claim['id'], 'failed', None, evidence='guess', caller=self.adapter)
        with self.assertRaises(ValueError):
            self.svc.reconcile_outbound(claim['id'], 'queued', None, evidence='guess', caller=self.op)
