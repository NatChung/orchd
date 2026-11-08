import tempfile
import unittest
from pathlib import Path
from orchd.channels import policy, service


class ServiceTest(unittest.TestCase):
    def test_duplicate_and_conflicting_receive(self):
        with tempfile.TemporaryDirectory() as d:
            svc = service.ChannelService(Path(d) / 'db')
            svc.provision_source({'id':'s','host_id':'h','provider':'slack','tenant_ref':'t','account_ref':'a','adapter_id':'adapter','enabled':True}, caller=policy.Caller(role='operator'))
            adapter = policy.Caller(role='adapter', sources=frozenset({'s'}), identity='adapter')
            event = {'schema_version':1,'transport_event_key':'e','origin':{'provider':'slack','tenant_ref':'t','conversation_ref':'c','message_id':'m'},'revision_key':'initial','conversation':{'kind':'channel','thread_key':''},'sender_ref':'sender','source_time':'2026-01-01T00:00:00Z','content':{'status':'complete','text':'synthetic','attachments':[]}}
            first = svc.receive('s', event, caller=adapter)
            self.assertEqual(svc.receive('s', event, caller=adapter)['status'], 'duplicate')
            event['content']['text'] = 'changed'
            with self.assertRaises(ValueError):
                svc.receive('s', event, caller=adapter)
            self.assertEqual(first['status'], 'received')
            svc.close()



    def test_cross_source_and_forged_identity_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            svc = service.ChannelService(Path(d) / 'db')
            try:
                operator=policy.Caller(role='operator')
                svc.provision_source({'id':'s','host_id':'h','provider':'slack','tenant_ref':'t','account_ref':'a','adapter_id':'adapter','enabled':True}, caller=operator)
                event = {'schema_version':1,'transport_event_key':'e','origin':{'provider':'slack','tenant_ref':'t','conversation_ref':'c','message_id':'m'},'revision_key':'initial','conversation':{'kind':'channel','thread_key':''},'sender_ref':'sender','source_time':'2026-01-01T00:00:00Z','content':{'status':'complete','text':'synthetic','attachments':[]}}
                adapter=policy.Caller(role='adapter', sources=frozenset({'s'}),identity='adapter')
                receipt=svc.receive('s',event,caller=adapter)
                outsider=policy.Caller(role='worker',sources=frozenset({'other'}))
                with self.assertRaises(PermissionError):
                    svc.message(receipt['message_id'],caller=outsider)
                with self.assertRaises(PermissionError):
                    svc.provision_source({'id':'x','host_id':'h','provider':'slack','account_ref':'a','adapter_id':'x'},caller=outsider)
                for field in ('actor','operator','approval','owner_orch','trust'):
                    forged=dict(event, **{field:'operator'})
                    with self.subTest(field=field), self.assertRaises(ValueError):
                        svc.receive('s',forged,caller=adapter)
                with self.assertRaises(PermissionError):
                    svc.approve(caller=outsider)
            finally:
                svc.close()

    def test_stale_search_and_sync_are_local_and_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            svc=service.ChannelService(Path(d)/'db')
            try:
                svc.provision_source({'id':'s','host_id':'h','provider':'gmail','account_ref':'a','adapter_id':'adapter','enabled':True}, caller=policy.Caller(role='operator'))
                caller=policy.Caller(role='worker',sources=frozenset({'s'}),identity='worker')
                from unittest.mock import patch
                with patch('socket.socket', side_effect=AssertionError('network is forbidden')):
                    self.assertEqual(svc.search('s','absent',caller=caller)['status'],'sync_required')
                first=svc.request_sync('s','key',{'window':'bounded'},caller=caller)
                second=svc.request_sync('s','key',{'window':'bounded'},caller=caller)
                self.assertEqual(first,second)
                with self.assertRaises(ValueError):
                    svc.request_sync('s','key',{'window':'different'},caller=caller)
            finally:
                svc.close()

    def test_delete_tombstones_content_immediately(self):
        with tempfile.TemporaryDirectory() as d:
            svc=service.ChannelService(Path(d)/'db')
            try:
                svc.provision_source({'id':'s','host_id':'h','provider':'slack','tenant_ref':'t','account_ref':'a','adapter_id':'adapter','enabled':True},caller=policy.Caller(role='operator'))
                adapter=policy.Caller(role='adapter',sources=frozenset({'s'}),identity='adapter')
                worker=policy.Caller(role='worker',sources=frozenset({'s'}))
                event={'schema_version':1,'transport_event_key':'e','origin':{'provider':'slack','tenant_ref':'t','conversation_ref':'c','message_id':'m'},'revision_key':'initial','conversation':{'kind':'channel','thread_key':''},'sender_ref':'sender','source_time':'2026-01-01T00:00:00Z','content':{'status':'complete','text':'synthetic','attachments':[]}}
                receipt=svc.receive('s',event,caller=adapter)
                svc.delete_message(receipt['message_id'],caller=adapter)
                value=svc.message(receipt['message_id'],caller=worker)
                self.assertIsNone(value['content']['text'])
                self.assertEqual(value['content']['status'],'unavailable')
                later=dict(event, transport_event_key='late', revision_key='late')
                with self.assertRaises(ValueError):
                    svc.receive('s',later,caller=adapter)
            finally:
                svc.close()


    def test_expired_body_is_tombstoned_at_read(self):
        from datetime import datetime, timezone
        with tempfile.TemporaryDirectory() as d:
            svc=service.ChannelService(Path(d)/'db', clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc))
            try:
                svc.provision_source({'id':'s','host_id':'h','provider':'slack','tenant_ref':'t','account_ref':'a','adapter_id':'adapter','enabled':True},caller=policy.Caller(role='operator'))
                adapter=policy.Caller(role='adapter',sources=frozenset({'s'}),identity='adapter')
                worker=policy.Caller(role='worker',sources=frozenset({'s'}))
                event={'schema_version':1,'transport_event_key':'e','origin':{'provider':'slack','tenant_ref':'t','conversation_ref':'c','message_id':'m'},'revision_key':'initial','conversation':{'kind':'channel','thread_key':''},'sender_ref':'sender','source_time':'2026-01-01T00:00:00Z','content':{'status':'complete','text':'synthetic','attachments':[]}}
                receipt=svc.receive('s',event,caller=adapter)
                svc.clock=lambda: datetime(2026, 4, 2, tzinfo=timezone.utc)
                self.assertIsNone(svc.message(receipt['message_id'],caller=worker)['content']['text'])
            finally:
                svc.close()

    def test_draft_is_local_and_outbound_disabled(self):
        with tempfile.TemporaryDirectory() as d:
            svc=service.ChannelService(Path(d)/'db')
            try:
                svc.provision_source({'id':'s','host_id':'h','provider':'slack','tenant_ref':'t','account_ref':'a','adapter_id':'adapter','enabled':True},caller=policy.Caller(role='operator'))
                adapter=policy.Caller(role='adapter',sources=frozenset({'s'}),identity='adapter')
                worker=policy.Caller(role='worker',sources=frozenset({'s'}),identity='worker')
                event={'schema_version':1,'transport_event_key':'e','origin':{'provider':'slack','tenant_ref':'t','conversation_ref':'c','message_id':'m'},'revision_key':'initial','conversation':{'kind':'channel','thread_key':''},'sender_ref':'sender','source_time':'2026-01-01T00:00:00Z','content':{'status':'complete','text':'synthetic','attachments':[]}}
                receipt=svc.receive('s',event,caller=adapter)
                result=svc.draft_reply(receipt['message_id'],{'sender':'s','target':'c','body':'synthetic reply','expires_at':2000000000},caller=worker)
                self.assertEqual(result['status'],'drafted')
                from orchd import intake
                draft=intake.show_draft(svc.con,result['draft_id'],result['version'])
                approval={key:draft[key] for key in ('draft_id','version','body_sha256','sender','target','source_ref')}
                approval['idempotency_key']='synthetic-key'
                with self.assertRaises(intake.IntakeError):
                    intake.approve(svc.con,approval,actor='operator')
                with self.assertRaises(intake.IntakeError):
                    intake.claim(svc.con,result['draft_id'],result['version'])
                with self.assertRaises(PermissionError):
                    svc.claim_outbound(caller=adapter)
            finally:
                svc.close()


    def test_v1_legacy_content_is_unavailable_and_link_is_idempotent(self):
        from orchd import intake, store
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'db'
            con=store.connect(path)
            event={'version':1,'provider':'slack','account':'a','event_id':'legacy','source_ref':'c','source_time':1700000000,'summary':'summary only'}
            receipt=intake.submit(con,event,producer='producer',producers={'producer':{'providers':['slack'],'accounts':['a']}})
            con.close()
            svc=service.ChannelService(path)
            try:
                op=policy.Caller(role='operator',sources=frozenset({'s'}))
                svc.provision_source({'id':'s','host_id':'h','provider':'slack','tenant_ref':'t','account_ref':'a','adapter_id':'adapter','enabled':True},caller=op)
                first=svc.link_legacy_intake('s',receipt['receipt_id'],caller=op)
                self.assertEqual(first,svc.link_legacy_intake('s',receipt['receipt_id'],caller=op))
                result=svc.message(first['message_id'],caller=op)
                self.assertEqual(result['content']['status'],'unavailable')
                self.assertIsNone(result['content']['text'])
            finally:
                svc.close()

    def test_snapshot_never_contains_channel_body(self):
        import json
        from orchd import intake
        with tempfile.TemporaryDirectory() as d:
            svc=service.ChannelService(Path(d)/'db')
            try:
                svc.provision_source({'id':'s','host_id':'h','provider':'slack','tenant_ref':'t','account_ref':'a','adapter_id':'adapter','enabled':True},caller=policy.Caller(role='operator'))
                event={'schema_version':1,'transport_event_key':'e','origin':{'provider':'slack','tenant_ref':'t','conversation_ref':'c','message_id':'m'},'revision_key':'initial','conversation':{'kind':'channel','thread_key':''},'sender_ref':'sender','source_time':'2026-01-01T00:00:00Z','content':{'status':'complete','text':'synthetic private marker','attachments':[]}}
                svc.receive('s',event,caller=policy.Caller(role='adapter',sources=frozenset({'s'}),identity='adapter'))
                tables={row[0] for row in svc.con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                snapshot=intake.project(svc.con,tables,0)
                self.assertNotIn('synthetic private marker',json.dumps(snapshot))
            finally:
                svc.close()

    def test_legacy_request_and_reply_idempotency_through_service(self):
        from orchd import store
        from tests.test_orchd import FakeRuntime
        with tempfile.TemporaryDirectory() as d:
            con=store.connect(Path(d)/'db')
            try:
                store.register_orch(con,'orch-A','codex')
                rt=FakeRuntime()
                registration=service.legacy_interface(con,'register',payload={'name':'adapter','orch_id':'orch-A'})
                auth={'name':'adapter','token':registration['token']}
                request=dict(auth,conversation='c',request_key='k',text='synthetic')
                first=service.legacy_interface(con,'submit',runtime=rt,payload=request)
                again=service.legacy_interface(con,'submit',runtime=rt,payload=request)
                self.assertEqual(first['request_id'],again['request_id'])
                self.assertTrue(again['duplicate'])
                reply={'request_id':first['request_id'],'text':'synthetic reply'}
                service.legacy_interface(con,'reply',actor='orch-A',payload=reply)
                service.legacy_interface(con,'reply',actor='orch-A',payload=reply)
                with self.assertRaises(ValueError):
                    service.legacy_interface(con,'reply',actor='orch-A',payload=dict(reply,text='changed'))
            finally:
                con.close()

    def test_unsend_deletes_cached_attachment_and_blocks_read(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/'channel'
            svc=service.ChannelService(Path(d)/'db',channel_home=root)
            try:
                svc.provision_source({'id':'s','host_id':'h','provider':'slack','tenant_ref':'t','account_ref':'a','adapter_id':'adapter','enabled':True},caller=policy.Caller(role='operator'))
                adapter=policy.Caller(role='adapter',sources=frozenset({'s'}),identity='adapter')
                worker=policy.Caller(role='worker',sources=frozenset({'s'}))
                event={'schema_version':1,'transport_event_key':'e','origin':{'provider':'slack','tenant_ref':'t','conversation_ref':'c','message_id':'m'},'revision_key':'initial','conversation':{'kind':'channel','thread_key':''},'sender_ref':'sender','source_time':'2026-01-01T00:00:00Z','content':{'status':'complete','text':'synthetic','attachments':[{'name':'synthetic.txt','status':'referenced','provider_attachment_ref':'att'}]}}
                receipt=svc.receive('s',event,caller=adapter)
                attachment=svc.con.execute('SELECT id FROM channel_attachments').fetchone()[0]
                key='a'*64
                path=root/'slack'/'state'/'s'/'content'/key
                path.parent.mkdir(parents=True)
                path.write_bytes(b'synthetic')
                svc.con.execute("UPDATE channel_attachments SET status='available',sha256=?,storage_key=? WHERE id=?",(key,key,attachment))
                svc.delete_message(receipt['message_id'],caller=adapter)
                self.assertFalse(path.exists())
                self.assertEqual(svc.read_attachment(attachment,caller=worker)['status'],'unavailable')
            finally:
                svc.close()

    def test_suggestion_updates_do_not_change_event_identity(self):
        with tempfile.TemporaryDirectory() as d:
            svc=service.ChannelService(Path(d)/'db')
            try:
                op=policy.Caller(role='operator',sources=frozenset({'s'}))
                svc.provision_source({'id':'s','host_id':'h','provider':'slack','tenant_ref':'t','account_ref':'a','adapter_id':'adapter','enabled':True},caller=op)
                adapter=policy.Caller(role='adapter',sources=frozenset({'s'}),identity='adapter')
                event={'schema_version':1,'transport_event_key':'e','origin':{'provider':'slack','tenant_ref':'t','conversation_ref':'c','message_id':'m'},'revision_key':'initial','conversation':{'kind':'channel','thread_key':''},'sender_ref':'sender','source_time':'2026-01-01T00:00:00Z','content':{'status':'complete','text':'synthetic','attachments':[],'mentions':['demo']},'suggestion':{'triage':{'urgency':'normal'}}}
                first=svc.receive('s',event,caller=adapter)
                event['suggestion']={'triage':{'urgency':'urgent'}}
                again=svc.receive('s',event,caller=adapter)
                self.assertEqual(first['receipt_id'],again['receipt_id'])
                self.assertEqual(again['status'],'duplicate')
                self.assertEqual(svc.con.execute('SELECT COUNT(*) FROM intake_suggestions WHERE receipt_id=?',(first['receipt_id'],)).fetchone()[0],2)
                event['content']['mentions']=['other']
                with self.assertRaises(ValueError):
                    svc.receive('s',event,caller=adapter)
            finally:
                svc.close()

    def test_legacy_submit_uses_mapped_channel_atomically(self):
        from orchd import store
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'db'
            svc=service.ChannelService(path)
            svc.provision_source({'id':'s','host_id':'h','provider':'slack','tenant_ref':'t','account_ref':'a','adapter_id':'producer','enabled':True},caller=policy.Caller(role='operator'))
            svc.close()
            con=store.connect(path)
            try:
                event={'version':1,'provider':'slack','account':'a','event_id':'legacy','source_ref':'c','source_time':1700000000,'summary':'summary only'}
                grants={'producer':{'providers':['slack'],'accounts':['a']}}
                first=service.legacy_intake_submit(con,event,producer='producer',producers=grants)
                again=service.legacy_intake_submit(con,event,producer='producer',producers=grants)
                self.assertEqual(first['receipt_id'],again['receipt_id'])
                self.assertEqual(again['status'],'duplicate')
                self.assertEqual(con.execute('SELECT COUNT(*) FROM channel_messages').fetchone()[0],1)
                from datetime import datetime, timezone
                svc=service.ChannelService(path)
                try:
                    normalized={'schema_version':1,'transport_event_key':'legacy','origin':{'provider':'slack','tenant_ref':'t','conversation_ref':'c','message_id':'legacy'},'revision_key':'initial','conversation':{'kind':'channel','thread_key':''},'sender_ref':'producer','source_time':datetime.fromtimestamp(1700000000,timezone.utc).isoformat(),'content':{'status':'unavailable','text':None,'unavailable_reason':'legacy_v1','attachments':[]}}
                    replay=svc.receive('s',normalized,caller=policy.Caller(role='adapter',sources=frozenset({'s'}),identity='producer'))
                    self.assertEqual(replay['status'],'duplicate')
                    self.assertEqual(replay['receipt_id'],first['receipt_id'])
                finally:
                    svc.close()
                event['owner_orch']='forged'
                with self.assertRaises(ValueError):
                    service.legacy_intake_submit(con,event,producer='producer',producers=grants)
                from unittest.mock import patch
                failed=dict(event, event_id='failed')
                failed.pop('owner_orch')
                with patch('orchd.channels.repository.link_legacy_intake', side_effect=RuntimeError('synthetic failure')):
                    with self.assertRaises(RuntimeError):
                        service.legacy_intake_submit(con,failed,producer='producer',producers=grants)
                self.assertEqual(con.execute("SELECT COUNT(*) FROM intake_events WHERE event_id='failed'").fetchone()[0],0)
            finally:
                con.close()

    def test_mapped_interface_requests_share_channel_receipt(self):
        from orchd import store
        from tests.test_orchd import FakeRuntime
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'db'
            con=store.connect(path)
            store.register_orch(con,'orch-A','codex')
            rt=FakeRuntime()
            reg=service.legacy_interface(con,'register',payload={'name':'adapter','orch_id':'orch-A'})
            svc=service.ChannelService(path)
            try:
                op=policy.Caller(role='operator')
                svc.provision_source({'id':'s','host_id':'h','provider':'slack','tenant_ref':'t','account_ref':'a','adapter_id':'adapter','enabled':True},caller=op)
                svc.apply_routes([{'id':'r','source_id':'s','conversation_ref':'c','owner_orch':'orch-A'}],caller=op)
                payload={'name':'adapter','token':reg['token'],'conversation':'c','request_key':'key','text':'synthetic'}
                first=service.legacy_interface(con,'submit',runtime=rt,payload=payload)
                again=service.legacy_interface(con,'submit',runtime=rt,payload=payload)
                self.assertEqual(first['request_id'],again['request_id'])
                self.assertEqual(con.execute("SELECT COUNT(*) FROM channel_legacy_refs WHERE system='interface'").fetchone()[0],1)
                self.assertEqual(con.execute('SELECT COUNT(*) FROM channel_processing_claims').fetchone()[0],1)
                reply={'request_id':first['request_id'],'text':'synthetic reply'}
                from unittest.mock import patch
                with patch('orchd.channels.policy.caller_for_identity', return_value=policy.Caller(role='orch',sources=frozenset({'s'}),identity='orch-A')):
                    service.legacy_interface(con,'reply',actor='orch-A',payload=reply)
                    service.legacy_interface(con,'reply',actor='orch-A',payload=reply)
                with self.assertRaisesRegex(ValueError,'disabled'):
                    service.legacy_interface(con,'fetch',payload={'name':'adapter','token':reg['token'],'conversation':'c'})
            finally:
                svc.close()
                con.close()

    def test_documented_synthetic_event_fixture(self):
        import json
        fixture=Path(__file__).parent/'fixtures'/'channels'/'event.json'
        event=json.loads(fixture.read_text(encoding='utf-8'))
        with tempfile.TemporaryDirectory() as d:
            svc=service.ChannelService(Path(d)/'db')
            try:
                svc.provision_source({'id':'s','host_id':'h','provider':'slack','tenant_ref':'demo-tenant','account_ref':'a','adapter_id':'adapter','enabled':True},caller=policy.Caller(role='operator'))
                result=svc.receive('s',event,caller=policy.Caller(role='adapter',sources=frozenset({'s'}),identity='adapter'))
                self.assertEqual(result['status'],'received')
            finally:
                svc.close()

    def test_different_source_observation_is_quarantined_without_content_leak(self):
        with tempfile.TemporaryDirectory() as d:
            svc=service.ChannelService(Path(d)/'db')
            try:
                op=policy.Caller(role='operator')
                for sid in ('a','b'):
                    svc.provision_source({'id':sid,'host_id':'h','provider':'slack','tenant_ref':'t','account_ref':sid,'adapter_id':sid,'enabled':True},caller=op)
                event={'schema_version':1,'transport_event_key':'one','origin':{'provider':'slack','tenant_ref':'t','conversation_ref':'c','message_id':'m'},'revision_key':'initial','conversation':{'kind':'channel','thread_key':''},'sender_ref':'sender','source_time':'2026-01-01T00:00:00Z','content':{'status':'complete','text':'first content','attachments':[]}}
                first=svc.receive('a',event,caller=policy.Caller(role='adapter',sources=frozenset({'a'}),identity='a'))
                event['transport_event_key']='two'
                event['content']['text']='different visibility'
                second=svc.receive('b',event,caller=policy.Caller(role='adapter',sources=frozenset({'b'}),identity='b'))
                self.assertEqual(second['status'],'conflict')
                self.assertTrue(second['review_required'])
                self.assertEqual(svc.con.execute('SELECT COUNT(*) FROM channel_observations').fetchone()[0],2)
                with self.assertRaises(PermissionError):
                    svc.message(first['message_id'],caller=policy.Caller(role='worker',sources=frozenset({'b'})))
            finally:
                svc.close()


    def test_sources_are_disabled_by_default(self):
        with tempfile.TemporaryDirectory() as d:
            svc=service.ChannelService(Path(d)/'db')
            try:
                svc.provision_source({'id':'s','host_id':'h','provider':'slack','tenant_ref':'t','account_ref':'a','adapter_id':'adapter'},caller=policy.Caller(role='operator'))
                event={'schema_version':1,'transport_event_key':'e','origin':{'provider':'slack','tenant_ref':'t','conversation_ref':'c','message_id':'m'},'revision_key':'initial','conversation':{'kind':'channel','thread_key':''},'sender_ref':'sender','source_time':'2026-01-01T00:00:00Z','content':{'status':'complete','text':'synthetic','attachments':[]}}
                with self.assertRaises(PermissionError):
                    svc.receive('s',event,caller=policy.Caller(role='adapter',sources=frozenset({'s'}),identity='adapter'))
            finally:
                svc.close()

    def test_channel_backups_expire_without_removing_unrelated_backups(self):
        import os
        from datetime import datetime, timezone
        from orchd import entry
        with tempfile.TemporaryDirectory() as d:
            now=datetime(2026,5,1,tzinfo=timezone.utc)
            svc=service.ChannelService(Path(d)/'db',clock=lambda: now)
            try:
                backup_dir=Path(d)/'backups'
                backup=entry._backup(svc.con,backup_dir)
                other=backup_dir/'unrelated.db'
                other.write_bytes(b'synthetic')
                old=now.timestamp()-31*86400
                os.utime(backup,(old,old))
                os.utime(other,(old,old))
                svc.enforce_retention(caller=policy.Caller(role='operator'))
                self.assertFalse(backup.exists())
                self.assertTrue(other.exists())
            finally:
                svc.close()

    def test_expiry_does_not_delete_a_newer_revision(self):
        from datetime import datetime, timezone
        with tempfile.TemporaryDirectory() as d:
            svc=service.ChannelService(Path(d)/'db',clock=lambda: datetime(2026,1,1,tzinfo=timezone.utc))
            try:
                svc.provision_source({'id':'s','host_id':'h','provider':'slack','tenant_ref':'t','account_ref':'a','adapter_id':'adapter','enabled':True},caller=policy.Caller(role='operator'))
                adapter=policy.Caller(role='adapter',sources=frozenset({'s'}),identity='adapter')
                caller=policy.Caller(role='worker',sources=frozenset({'s'}))
                event={'schema_version':1,'transport_event_key':'e','origin':{'provider':'slack','tenant_ref':'t','conversation_ref':'c','message_id':'m'},'revision_key':'initial','conversation':{'kind':'channel','thread_key':''},'sender_ref':'sender','source_time':'2026-01-01T00:00:00Z','content':{'status':'complete','text':'old synthetic','attachments':[]}}
                old=svc.receive('s',event,caller=adapter)
                svc.clock=lambda: datetime(2026,4,2,tzinfo=timezone.utc)
                self.assertEqual(svc.message(old['message_id'],caller=caller)['content']['unavailable_reason'],'expired')
                event['transport_event_key']='new-event'
                event['revision_key']='new-revision'
                event['content']['text']='new synthetic'
                new=svc.receive('s',event,caller=adapter)
                self.assertEqual(svc.message(new['message_id'],caller=caller)['content']['text'],'new synthetic')
            finally:
                svc.close()
