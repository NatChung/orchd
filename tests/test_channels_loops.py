from channel_helpers import ChannelCase
from orchd.channels.loops import Automation

class LoopTest(ChannelCase):
    def test_two_apps_one_claim(self):
        a=self.source('a'); b=self.source('b')
        x=self.svc.receive('a',self.event(),caller=a)
        y=self.svc.receive('b',self.event('second'),caller=b)
        self.assertEqual(x['message_id'],y['message_id'])
        self.assertEqual(self.svc.con.execute('SELECT count(*) FROM channel_processing_claims').fetchone()[0],1)
    def test_automation_budget_and_hop_survive_restart(self):
        a=self.source()
        context=Automation(correlation_id='chain',verified=True,hop_count=1)
        for n in range(4):
            result=self.svc.receive('s',self.event(str(n),str(n)),caller=a,automation=context)
        self.svc.close(); self.open()
        result=self.svc.receive('s',self.event('last','last'),caller=a,automation=context)
        self.assertEqual(result['loop_status'],'budget_exhausted')
        result=self.svc.receive('s',self.event('hop','hop'),caller=a,automation=Automation(correlation_id='other',verified=True,hop_count=9))
        self.assertEqual(result['loop_status'],'hop_limit')
    def test_allowlisted_app_does_not_clear_budget_or_trust(self):
        a=self.source()
        result=self.svc.receive('s',self.event(),caller=a,automation=Automation(app_ref='allowed',verified=False))
        self.assertEqual(result['loop_status'],'unverified_automation')
        row=self.svc.message(result['message_id'],caller=a)
        self.assertEqual(row['trust'],'third_party_data')
        self.assertEqual(self.svc.con.execute('SELECT count(*) FROM channel_processing_claims').fetchone()[0],0)
    def test_suppressed_first_observation_cannot_be_promoted_by_second_app(self):
        a=self.source('a'); b=self.source('b')
        first=self.svc.receive('a',self.event(),caller=a,automation=Automation(app_ref='allowed'))
        self.svc.receive('b',self.event('second'),caller=b)
        self.assertEqual(self.svc.con.execute('SELECT count(*) FROM channel_processing_claims').fetchone()[0],0)
    def test_human_looking_proxy_does_not_reset_existing_budget(self):
        a=self.source()
        context=Automation(app_ref='allowed',verified=True,correlation_id='chain',hop_count=1)
        for n in range(4): self.svc.receive('s',self.event(str(n),str(n)),caller=a,automation=context)
        proxy=self.svc.receive('s',self.event('proxy','proxy'),caller=a,automation=Automation(app_ref='allowed'))
        self.assertEqual(proxy['loop_status'],'unverified_automation')
        self.svc.receive('s',self.event('human','human'),caller=a)
        self.svc.close(); self.open()
        result=self.svc.receive('s',self.event('last','last'),caller=a,automation=context)
        self.assertEqual(result['loop_status'],'budget_exhausted')
    def test_claim_failure_rolls_back_budget(self):
        a=self.source()
        self.svc.con.execute("CREATE TRIGGER fail_claim BEFORE INSERT ON channel_processing_claims BEGIN SELECT RAISE(ABORT,'synthetic'); END")
        import sqlite3
        context=Automation(verified=True,correlation_id='chain',hop_count=1)
        with self.assertRaises(sqlite3.IntegrityError): self.svc.receive('s',self.event(),caller=a,automation=context)
        self.assertEqual(self.svc.con.execute('SELECT count(*) FROM channel_loop_state').fetchone()[0],0)
    def test_sent_provider_receipt_blocks_human_looking_echo(self):
        a=self.source()
        first=self.svc.receive('s',self.event(),caller=a)
        from orchd.channels import policy
        owner=policy.Caller(role='orch',sources=frozenset({'s'}),identity='orch')
        draft=self.svc.draft_reply(first['message_id'],dict(sender='s',target='c',body='synthetic',expires_at=9999999999),caller=owner)
        # Fake delivery flow only: production claim/approve remain hard-disabled.
        self.svc.con.execute("UPDATE channel_deliveries SET kind='outbound',draft_id=?,draft_version=1 WHERE message_id=?",(draft['draft_id'],first['message_id']))
        self.svc.con.execute("INSERT INTO channel_delivery_attempts SELECT id,1,'provider_accepted','2026-01-02T00:00:00Z',NULL,?,NULL FROM channel_deliveries WHERE message_id=?",('{"conversation_ref":"c","message_id":"echo"}',first['message_id']))
        result=self.svc.receive('s',self.event('echo','echo'),caller=a)
        self.assertEqual(result['loop_status'],'sent_echo')
        self.assertEqual(self.svc.con.execute('SELECT count(*) FROM channel_processing_claims').fetchone()[0],1)
