import unittest
from orchd.channels import routing


class RoutingTest(unittest.TestCase):
    def test_overlapping_routes_are_rejected(self):
        routes = [{'id':'a','owner_orch':'o','source_id':'s','conversation_ref':'c','senders':[]}, {'id':'b','owner_orch':'o','source_id':'s','conversation_ref':'c','senders':['person']}]
        with self.assertRaises(ValueError):
            routing.validate_routes(routes)

    def test_rejects_overlapping_batch_before_writing(self):
        import tempfile
        from pathlib import Path
        from orchd import store
        from orchd.channels import policy, service
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'db'
            con=store.connect(path)
            con.execute("INSERT INTO orchs(id,kind,created_at) VALUES('orch','codex',0)")
            con.close()
            svc=service.ChannelService(path)
            try:
                op=policy.Caller(role='operator')
                svc.provision_source({'id':'s','host_id':'h','provider':'slack','tenant_ref':'t','account_ref':'a','adapter_id':'adapter','enabled':True},caller=op)
                routes=[{'id':'r1','source_id':'s','conversation_ref':'c','owner_orch':'orch','senders':[]}, {'id':'r2','source_id':'s','conversation_ref':'c','owner_orch':'orch','senders':['x']}]
                with self.assertRaises(ValueError):
                    svc.apply_routes(routes,caller=op)
                self.assertEqual(svc.routes(caller=op),[])
            finally:
                svc.close()

    def test_assigned_event_and_outbox_commit_together(self):
        import tempfile
        from pathlib import Path
        from orchd import store
        from orchd.channels import policy, service
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'db'
            con=store.connect(path)
            con.execute("INSERT INTO orchs(id,kind,created_at) VALUES('orch','codex',0)")
            con.close()
            svc=service.ChannelService(path)
            try:
                op=policy.Caller(role='operator')
                svc.provision_source({'id':'s','host_id':'h','provider':'slack','tenant_ref':'t','account_ref':'a','adapter_id':'adapter','enabled':True},caller=op)
                svc.apply_routes([{'id':'r','source_id':'s','conversation_ref':'c','owner_orch':'orch','senders':['sender']}],caller=op)
                event={'schema_version':1,'transport_event_key':'e','origin':{'provider':'slack','tenant_ref':'t','conversation_ref':'c','message_id':'m'},'revision_key':'initial','conversation':{'kind':'channel','thread_key':''},'sender_ref':'sender','source_time':'2026-01-01T00:00:00Z','content':{'status':'complete','text':'synthetic','attachments':[]}}
                receipt=svc.receive('s',event,caller=policy.Caller(role='adapter',sources=frozenset({'s'}),identity='adapter'))
                self.assertEqual(svc.con.execute('SELECT owner_orch FROM intake_events WHERE receipt_id=?',(receipt['receipt_id'],)).fetchone()[0],'orch')
                self.assertEqual(svc.con.execute("SELECT COUNT(*) FROM channel_deliveries WHERE kind='wake'").fetchone()[0],1)
                outsider=policy.Caller(role='orch',sources=frozenset({'s'}),identity='other')
                with self.assertRaises(PermissionError):
                    svc.message(receipt['message_id'],caller=outsider)
                svc.con.execute("CREATE TRIGGER synthetic_failure BEFORE INSERT ON channel_deliveries BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END")
                failed=dict(event, transport_event_key='failed', origin=dict(event['origin'], message_id='failed'))
                import sqlite3
                with self.assertRaises(sqlite3.IntegrityError):
                    svc.receive('s',failed,caller=policy.Caller(role='adapter',sources=frozenset({'s'}),identity='adapter'))
                self.assertEqual(svc.con.execute("SELECT COUNT(*) FROM channel_origins WHERE provider_message_id='failed'").fetchone()[0],0)
                self.assertEqual(svc.con.execute("SELECT COUNT(*) FROM intake_events WHERE event_id='failed'").fetchone()[0],0)
            finally:
                svc.close()


    def test_observer_first_does_not_steal_ownership(self):
        import tempfile
        from pathlib import Path
        from orchd import store
        from orchd.channels import policy, service
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'db'
            con=store.connect(path)
            con.execute("INSERT INTO orchs(id,kind,created_at) VALUES('orch','codex',0)")
            con.close()
            svc=service.ChannelService(path)
            try:
                op=policy.Caller(role='operator')
                for sid in ('observer','owner'):
                    svc.provision_source({'id':sid,'host_id':'h','provider':'slack','tenant_ref':'t','account_ref':sid,'adapter_id':sid,'enabled':True},caller=op)
                svc.apply_routes([{'id':'observer-route','source_id':'observer','conversation_ref':'c','owner_orch':'orch','mode':'observer'}, {'id':'owner-route','source_id':'owner','conversation_ref':'c','owner_orch':'orch','mode':'receive_only'}],caller=op)
                event={'schema_version':1,'transport_event_key':'first','origin':{'provider':'slack','tenant_ref':'t','conversation_ref':'c','message_id':'m'},'revision_key':'initial','conversation':{'kind':'channel','thread_key':''},'sender_ref':'sender','source_time':'2026-01-01T00:00:00Z','content':{'status':'complete','text':'synthetic','attachments':[]}}
                first=svc.receive('observer',event,caller=policy.Caller(role='adapter',sources=frozenset({'observer'}),identity='observer'))
                event['transport_event_key']='second'
                second=svc.receive('owner',event,caller=policy.Caller(role='adapter',sources=frozenset({'owner'}),identity='owner'))
                self.assertEqual(first['message_id'],second['message_id'])
                self.assertEqual(svc.con.execute('SELECT owner_orch FROM intake_events WHERE receipt_id=?',(first['receipt_id'],)).fetchone()[0],'orch')
                self.assertEqual(svc.con.execute('SELECT COUNT(*) FROM channel_processing_claims').fetchone()[0],1)
                svc.delete_message(first['message_id'],caller=policy.Caller(role='adapter',sources=frozenset({'observer'}),identity='observer'))
                self.assertIsNone(svc.message(first['message_id'],caller=policy.Caller(role='orch',sources=frozenset({'owner'}),identity='orch'))['content']['text'])
            finally:
                svc.close()
