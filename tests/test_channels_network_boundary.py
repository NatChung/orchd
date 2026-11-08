import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from orchd.channels import policy, service
from orchd.channels.adapters.email import EmailAdapter
from orchd.channels.secret_store import FakeSecretStore

class FakeGmail:
    def __init__(self): self.calls=[]
    def list_messages(self,start,end,page_token=None): self.calls.append('list'); return {'messages':[{'id':'m'}]}
    def get_message(self,id):
        self.calls.append('get')
        return {'id':id,'threadId':'t','internalDate':'1767225600000','snippet':'synthetic','payload':{'mimeType':'multipart/mixed','parts':[{'filename':'a.txt','body':{'attachmentId':'att','size':4}}]}}
    def get_attachment(self,id,ref): self.calls.append('attachment'); return b'data'

class NetworkBoundaryTest(unittest.TestCase):
    def test_agent_query_read_and_attachment_never_call_platform(self):
        now=datetime(2026,1,2,tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as d:
            svc=service.ChannelService(Path(d)/'db',clock=lambda:now,channel_home=Path(d)/'channel')
            try:
                svc.provision_source({'id':'s','host_id':'h','provider':'gmail','account_ref':'a','adapter_id':'adapter','enabled':True,'credential_refs':{'read':'os-store:email/read'}},caller=policy.Caller(role='operator'))
                adapter=policy.Caller(role='adapter',sources=frozenset({'s'}),identity='adapter')
                worker=policy.Caller(role='worker',sources=frozenset({'s'}),identity='worker')
                fake=FakeGmail(); secret=FakeSecretStore({('adapter','s','os-store:email/read'):b'fake'})
                poller=EmailAdapter(svc,'s',adapter,fake,secret,activation=datetime(2026,1,1,tzinfo=timezone.utc),clock=lambda:now)
                poller.poll()
                self.assertEqual(fake.calls,['list','get'])
                with patch('socket.socket',side_effect=AssertionError('network forbidden')):
                    result=svc.search('s','synthetic',caller=worker)
                    message=svc.message(result['messages'][0]['message_id'],caller=worker)
                    aid=message['content']['attachments'][0]['attachment_id']
                    self.assertEqual(svc.request_attachment(aid,caller=worker)['status'],'queued')
                    self.assertEqual(svc.read_attachment(aid,caller=worker)['status'],'adapter_unavailable')
                    svc.request_sync('s','request',{},caller=worker)
                self.assertEqual(fake.calls,['list','get'])
                poller.run_pending()
                self.assertEqual(fake.calls,['list','get','attachment','list','get'])
                with patch('socket.socket',side_effect=AssertionError('network forbidden')):
                    self.assertEqual(svc.read_attachment(aid,caller=worker)['content_base64'],'ZGF0YQ==')
                self.assertEqual(fake.calls,['list','get','attachment','list','get'])
            finally: svc.close()
    def test_inventory_keeps_platform_sdk_inside_adapter(self):
        import ast
        root=Path(__file__).resolve().parents[1]/'orchd'
        offenders=[]
        for path in root.rglob('*.py'):
            if 'adapters' in path.parts: continue
            tree=ast.parse(path.read_text(encoding='utf-8-sig'))
            for node in ast.walk(tree):
                if isinstance(node,ast.Import): names=[a.name for a in node.names]
                elif isinstance(node,ast.ImportFrom): names=[node.module or '']
                else: continue
                if any(name.startswith(('googleapiclient','google.oauth2','imaplib')) for name in names): offenders.append(str(path))
        self.assertEqual(offenders,[])
        from orchd import mcp_server
        names={item['name'] for item in mcp_server.TOOLS}
        self.assertFalse(names.intersection({'gmail_search','gmail_get','gmail_send','gmail_modify','imap_search'}))
