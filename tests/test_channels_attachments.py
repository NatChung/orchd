import tempfile
import unittest
from pathlib import Path
from orchd.channels.attachments import content_path

class AttachmentTest(unittest.TestCase):
    def test_traversal_and_invalid_digest_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                content_path(Path(d), '../other', 'a'*64)
            with self.assertRaises(ValueError):
                content_path(Path(d), 's', '../file')
    def test_attachment_is_queued_and_read_locally_with_acl(self):
        from orchd.channels import policy, service
        from datetime import datetime, timezone
        with tempfile.TemporaryDirectory() as d:
            now=datetime(2026,1,2,tzinfo=timezone.utc)
            svc=service.ChannelService(Path(d)/'db',clock=lambda:now,channel_home=Path(d)/'channel')
            try:
                svc.provision_source({'id':'s','host_id':'h','provider':'gmail','account_ref':'a','adapter_id':'adapter','enabled':True},caller=policy.Caller(role='operator'))
                adapter=policy.Caller(role='adapter',sources=frozenset({'s'}),identity='adapter')
                worker=policy.Caller(role='worker',sources=frozenset({'s'}))
                outsider=policy.Caller(role='worker',sources=frozenset({'other'}))
                event={'schema_version':1,'transport_event_key':'m','origin':{'provider':'gmail','tenant_ref':'','conversation_ref':'t','message_id':'m'},'revision_key':'initial','conversation':{'kind':'email','thread_key':'t'},'sender_ref':'sender','source_time':now.isoformat(),'content':{'status':'complete','text':'hello','attachments':[{'name':'report.txt','status':'referenced','bytes':4,'provider_attachment_ref':'a'}]}}
                receipt=svc.receive('s',event,caller=adapter)
                view=svc.message(receipt['message_id'],caller=worker)
                attachment_id=view['content']['attachments'][0]['attachment_id']
                self.assertEqual(svc.request_attachment(attachment_id,caller=worker)['status'],'queued')
                generation=svc.acquire_source_lease('s','holder',caller=adapter)
                svc.store_attachment(attachment_id,b'data','holder',generation,caller=adapter)
                svc.release_source_lease('s','holder',generation,caller=adapter)
                self.assertEqual(svc.read_attachment(attachment_id,caller=worker)['content_base64'],'ZGF0YQ==')
                with self.assertRaises(PermissionError): svc.read_attachment(attachment_id,caller=outsider)
            finally: svc.close()
    def test_adapter_fetches_only_requested_attachment(self):
        from orchd.channels import policy, service
        from orchd.channels.adapters.email import EmailAdapter
        from orchd.channels.secret_store import FakeSecretStore
        from datetime import datetime, timezone
        now=datetime(2026,1,2,tzinfo=timezone.utc)
        class Transport:
            def __init__(self): self.calls=[]
            def get_attachment(self,message_id,ref): self.calls.append(('attachment',message_id,ref)); return b'data'
            def list_messages(self,start,end,page_token=None): self.calls.append(('list',)); return {}
        with tempfile.TemporaryDirectory() as d:
            svc=service.ChannelService(Path(d)/'db',clock=lambda:now,channel_home=Path(d)/'channel')
            try:
                svc.provision_source({'id':'s','host_id':'h','provider':'gmail','account_ref':'a','adapter_id':'adapter','enabled':True,'credential_refs':{'read':'os-store:email/read'}},caller=policy.Caller(role='operator'))
                adapter=policy.Caller(role='adapter',sources=frozenset({'s'}),identity='adapter')
                worker=policy.Caller(role='worker',sources=frozenset({'s'}))
                event={'schema_version':1,'transport_event_key':'m','origin':{'provider':'gmail','tenant_ref':'','conversation_ref':'t','message_id':'m'},'revision_key':'initial','conversation':{'kind':'email','thread_key':'t'},'sender_ref':'sender','source_time':now.isoformat(),'content':{'status':'complete','text':'hello','attachments':[{'name':'report.txt','status':'referenced','bytes':4,'provider_attachment_ref':'a'}]}}
                receipt=svc.receive('s',event,caller=adapter)
                aid=svc.message(receipt['message_id'],caller=worker)['content']['attachments'][0]['attachment_id']
                svc.request_attachment(aid,caller=worker)
                transport=Transport(); secrets=FakeSecretStore({('adapter','s','os-store:email/read'):b'fake'})
                EmailAdapter(svc,'s',adapter,transport,secrets,activation=now,clock=lambda:now).poll()
                self.assertEqual(transport.calls,[('attachment','m','a'),('list',)])
                self.assertEqual(svc.read_attachment(aid,caller=worker)['content_base64'],'ZGF0YQ==')
            finally: svc.close()
    def test_size_limit_and_expired_lease_do_not_publish(self):
        from datetime import datetime, timedelta, timezone
        from unittest.mock import patch
        from orchd.channels import policy, service
        now=datetime(2026,1,2,tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as d:
            svc=service.ChannelService(Path(d)/'db',clock=lambda:now,channel_home=Path(d)/'channel')
            try:
                svc.provision_source({'id':'s','host_id':'h','provider':'gmail','account_ref':'a','adapter_id':'adapter','enabled':True},caller=policy.Caller(role='operator'))
                adapter=policy.Caller(role='adapter',sources=frozenset({'s'}),identity='adapter')
                worker=policy.Caller(role='worker',sources=frozenset({'s'}))
                event={'schema_version':1,'transport_event_key':'m','origin':{'provider':'gmail','tenant_ref':'','conversation_ref':'t','message_id':'m'},'revision_key':'initial','conversation':{'kind':'email','thread_key':'t'},'sender_ref':'sender','source_time':now.isoformat(),'content':{'status':'complete','text':'hello','attachments':[{'name':'../report.txt','status':'referenced','bytes':4,'provider_attachment_ref':'a'}]}}
                receipt=svc.receive('s',event,caller=adapter)
                aid=svc.message(receipt['message_id'],caller=worker)['content']['attachments'][0]['attachment_id']
                svc.request_attachment(aid,caller=worker)
                gen=svc.acquire_source_lease('s','holder',caller=adapter,seconds=1)
                with patch('orchd.channels.attachments.MAX_ATTACHMENT_BYTES',3), self.assertRaises(ValueError):
                    svc.store_attachment(aid,b'data','holder',gen,caller=adapter)
                svc.clock=lambda:now+timedelta(seconds=2)
                with self.assertRaises(PermissionError): svc.store_attachment(aid,b'data','holder',gen,caller=adapter)
                self.assertEqual(svc.read_attachment(aid,caller=worker)['status'],'adapter_unavailable')
                self.assertFalse(any((Path(d)/'channel').rglob('3a6eb0790f39ac87c94f3856b2dd2c5d110e6811602261a9a923d3bb23adc8b7')))
            finally: svc.close()
