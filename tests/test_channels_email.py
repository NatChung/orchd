import base64
import unittest
from orchd.channels.adapters.email import normalize_message


def encoded(text):
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip('=')


class EmailTest(unittest.TestCase):
    def test_full_mime_and_snippet(self):
        raw = {'id':'m1','threadId':'t','internalDate':'1767225600000', 'snippet':'preview',
               'payload':{'mimeType':'text/plain','headers':[{'name':'From','value':'sender@example.invalid'}],
                          'body':{'data':encoded('full synthetic body')}}}
        event = normalize_message(raw, tenant_ref='')
        self.assertEqual(event['content']['status'], 'complete')
        self.assertEqual(event['content']['text'], 'full synthetic body')
        raw.pop('payload')
        event = normalize_message(raw, tenant_ref='')
        self.assertEqual(event['content']['status'], 'truncated')
        self.assertEqual(event['content']['text'], 'preview')

    def test_same_thread_has_distinct_message_identity(self):
        base = {'threadId':'t','internalDate':'1767225600000','snippet':'x'}
        a = normalize_message(dict(base,id='a'), tenant_ref='')
        b = normalize_message(dict(base,id='b'), tenant_ref='')
        self.assertEqual(a['origin']['conversation_ref'], b['origin']['conversation_ref'])
        self.assertNotEqual(a['origin']['message_id'], b['origin']['message_id'])
    def test_mime_attachment_summary_and_size_limit(self):
        from orchd.channels.adapters.email import MAX_ATTACHMENT_BYTES
        raw={'id':'m','threadId':'t','internalDate':'1767225600000','payload':{'mimeType':'multipart/mixed','parts':[{'mimeType':'text/html','body':{'data':encoded('<p>Hello<script>ignored()</script> world</p>')}},{'filename':'large.zip','body':{'attachmentId':'ref','size':MAX_ATTACHMENT_BYTES+1}},{'filename':'small.txt','body':{'attachmentId':'ref2','size':12}}]}}
        content=normalize_message(raw,tenant_ref='')['content']
        self.assertEqual(content['status'],'complete')
        self.assertIn('Hello world',content['text'])
        self.assertNotIn('ignored',content['text'])
        self.assertEqual(content['attachments'][0]['status'],'unavailable')
        self.assertEqual(content['attachments'][1],{'name':'small.txt','status':'referenced','bytes':12,'provider_attachment_ref':'ref2'})
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from orchd.channels import policy, service
from orchd.channels.adapters.email import EmailAdapter
from orchd.channels.secret_store import FakeSecretStore

NOW=datetime(2026,1,2,tzinfo=timezone.utc)
class FakeGmail:
    def __init__(self):
        self.calls=[]; self.fail=False
    def list_messages(self, start, end, page_token=None):
        self.calls.append(('list',page_token))
        if page_token and self.fail: raise RuntimeError('page failed')
        return {'messages':[{'id':'a'}], 'nextPageToken':'next'} if not page_token else {'messages':[{'id':'b'}]}
    def get_message(self, message_id):
        self.calls.append(('get',message_id))
        return {'id':message_id, 'threadId':'thread', 'internalDate':'1767225600000','snippet':message_id}

class AdapterTest(unittest.TestCase):
    def test_failed_page_does_not_advance_and_restart_replays_without_loss(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'db'; channel=Path(d)/'channel'
            svc=service.ChannelService(path, clock=lambda:NOW, channel_home=channel)
            svc.provision_source({'id':'s','host_id':'h','provider':'gmail','account_ref':'a','adapter_id':'adapter','enabled':True,'credential_refs':{'read':'os-store:email/read'}}, caller=policy.Caller(role='operator'))
            fake=FakeGmail(); fake.fail=True
            caller=policy.Caller(role='adapter', sources=frozenset({'s'}), identity='adapter')
            worker=policy.Caller(role='worker',sources=frozenset({'s'}))
            store=FakeSecretStore({('adapter','s','os-store:email/read'):b'fake'})
            adapter=EmailAdapter(svc, 's', caller, fake, store, activation=datetime(2026,1,1,tzinfo=timezone.utc), clock=lambda:NOW)
            with self.assertRaises(RuntimeError): adapter.poll()
            self.assertIsNone(svc.checkpoint('s', 'mail', caller=caller))
            svc.close()
            svc=service.ChannelService(path, clock=lambda:NOW, channel_home=channel)
            try:
                fake.fail=False
                adapter=EmailAdapter(svc,'s',caller,fake,store,activation=datetime(2026,1,1,tzinfo=timezone.utc),clock=lambda:NOW)
                adapter.poll()
                self.assertEqual(len(svc.messages('s',caller=worker)['messages']),2)
                self.assertEqual(svc.checkpoint('s','mail',caller=caller)['committed_cursor'],NOW.isoformat())
            finally: svc.close()
    def test_request_sync_is_serviced_by_same_adapter(self):
        with tempfile.TemporaryDirectory() as d:
            svc=service.ChannelService(Path(d)/'db',clock=lambda:NOW,channel_home=Path(d)/'channel')
            try:
                svc.provision_source({'id':'s','host_id':'h','provider':'gmail','account_ref':'a','adapter_id':'adapter','enabled':True,'credential_refs':{'read':'os-store:email/read'}},caller=policy.Caller(role='operator'))
                caller=policy.Caller(role='adapter',sources=frozenset({'s'}),identity='adapter')
                worker=policy.Caller(role='worker',sources=frozenset({'s'}),identity='worker')
                store=FakeSecretStore({('adapter','s','os-store:email/read'):b'fake'})
                fake=FakeGmail()
                adapter=EmailAdapter(svc,'s',caller,fake,store,activation=datetime(2026,1,1,tzinfo=timezone.utc),clock=lambda:NOW)
                job=svc.request_sync('s','key',{},caller=worker)
                adapter.run_pending()
                self.assertEqual(svc.sync_status(job['job_id'],caller=worker)['state'],'succeeded')
                self.assertEqual(len(svc.messages('s',caller=worker)['messages']),2)
            finally: svc.close()

    def test_invalid_secret_degrades_health(self):
        with tempfile.TemporaryDirectory() as d:
            svc=service.ChannelService(Path(d)/'db',clock=lambda:NOW,channel_home=Path(d)/'channel')
            try:
                svc.provision_source({'id':'s','host_id':'h','provider':'gmail','account_ref':'a','adapter_id':'adapter','enabled':True,'credential_refs':{'read':'os-store:email/read'}},caller=policy.Caller(role='operator'))
                caller=policy.Caller(role='adapter',sources=frozenset({'s'}),identity='adapter')
                adapter=EmailAdapter(svc,'s',caller,FakeGmail(),FakeSecretStore({}),activation=datetime(2026,1,1,tzinfo=timezone.utc),clock=lambda:NOW)
                with self.assertRaises(ValueError): adapter.poll()
                self.assertEqual(adapter.health['status'],'degraded')
                self.assertTrue(adapter.health['credential_error'])
                self.assertIsNone(svc.checkpoint('s','mail',caller=caller))
            finally: svc.close()
    def test_transport_auth_failure_degrades_without_checkpoint(self):
        class AuthError(Exception):
            status_code=401
        class Denied(FakeGmail):
            def list_messages(self, *args, **kwargs): raise AuthError()
        with tempfile.TemporaryDirectory() as d:
            svc=service.ChannelService(Path(d)/'db',clock=lambda:NOW,channel_home=Path(d)/'channel')
            try:
                svc.provision_source({'id':'s','host_id':'h','provider':'gmail','account_ref':'a','adapter_id':'adapter','enabled':True,'credential_refs':{'read':'os-store:email/read'}},caller=policy.Caller(role='operator'))
                caller=policy.Caller(role='adapter',sources=frozenset({'s'}),identity='adapter')
                store=FakeSecretStore({('adapter','s','os-store:email/read'):b'fake'})
                adapter=EmailAdapter(svc,'s',caller,Denied(),store,activation=datetime(2026,1,1,tzinfo=timezone.utc),clock=lambda:NOW)
                with self.assertRaises(AuthError): adapter.poll()
                self.assertTrue(adapter.health['credential_error'])
                self.assertIsNone(svc.checkpoint('s','mail',caller=caller))
            finally: svc.close()
    def test_expired_history_replays_from_activation(self):
        from orchd.channels.adapters.email import HistoryExpired
        class Expired(FakeGmail):
            def __init__(self): super().__init__(); self.history=False; self.windows=[]
            def current_history_id(self): return '10' if not self.history else '20'
            def list_history(self, cursor, page_token=None):
                self.history=True
                raise HistoryExpired()
            def list_messages(self,start,end,page_token=None):
                self.windows.append(start)
                return super().list_messages(start,end,page_token)
        with tempfile.TemporaryDirectory() as d:
            svc=service.ChannelService(Path(d)/'db',clock=lambda:NOW,channel_home=Path(d)/'channel')
            try:
                svc.provision_source({'id':'s','host_id':'h','provider':'gmail','account_ref':'a','adapter_id':'adapter','enabled':True,'credential_refs':{'read':'os-store:email/read'}},caller=policy.Caller(role='operator'))
                caller=policy.Caller(role='adapter',sources=frozenset({'s'}),identity='adapter')
                store=FakeSecretStore({('adapter','s','os-store:email/read'):b'fake'})
                transport=Expired(); activation=datetime(2026,1,1,tzinfo=timezone.utc)
                adapter=EmailAdapter(svc,'s',caller,transport,store,activation=activation,clock=lambda:NOW)
                adapter.poll(); adapter.poll()
                self.assertTrue(transport.history)
                self.assertEqual(transport.windows[-2],activation)
                self.assertEqual(len(svc.messages('s',caller=policy.Caller(role='worker',sources=frozenset({'s'})))['messages']),2)
            finally: svc.close()
