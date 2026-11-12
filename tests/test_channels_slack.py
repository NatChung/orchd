from channel_helpers import ChannelCase
from orchd.channels.adapters.slack import normalize_slack

class SlackTest(ChannelCase):
    def test_edit_identity_and_timestamp_are_strings(self):
        payload={'team_id':'t','event_id':'edit','event':{'type':'message','subtype':'message_changed','channel':'c','event_ts':'1767225601.000001','message':{'ts':'1767225600.000001','user':'human','text':'edited','edited':{'ts':'1767225601.000001'}}}}
        e=normalize_slack(payload)
        self.assertEqual(e['origin']['message_id'],'1767225600.000001')
        self.assertEqual(e['revision_key'],'edit:1767225601.000001')
from orchd.channels.adapters.slack import SlackAdapter, SlackError, HistoryGap
from orchd.channels.secret_store import FakeSecretStore
from orchd.channels import policy

class FakeSlack:
    def __init__(self):
        self.acks=[]; self.calls=[]; self.sent=[]; self.failure=None; self.callback=None
    def auth_test(self, credential): return {'ok': True, 'team_id': 't', 'user_id': 'account'}
    def connect(self,credentials,callback): self.callback=callback; self.calls.append('connect')
    def ack(self,key): self.acks.append(key)
    def history(self,conversation,oldest,end,*,thread=None,cursor=None):
        self.calls.append((conversation,thread,cursor))
        if self.failure: raise self.failure
        if not thread:
            return {'messages':[{'ts':'1767225600.000001','user':'human','text':'synthetic','reply_count':1}]}
        if not cursor:
            return {'messages':[{'ts':'1767225601.000001','user':'human','text':'reply'}],'response_metadata':{'next_cursor':'next'}}
        return {'messages':[{'ts':'1767225602.000001','user':'human','text':'reply2'}]}
    def send(self,*args): self.sent.append(args)

def envelope(mid='1767225600.000001',key='event',**fields):
    return {'envelope_id':'env-'+key,'payload':{'team_id':'t','event_id':key,'event':dict(type='message',channel='c',ts=mid,user='human',text='synthetic',**fields)}}

class SocketTest(ChannelCase):
    def adapter(self):
        self.caller=self.source(); self.fake=FakeSlack()
        secret=FakeSecretStore({('s','s','os-store:slack/app'):b'fake-app',('s','s','os-store:slack/api'):b'fake-api'})
        return SlackAdapter(self.svc,'s',self.caller,self.fake,secret,conversations=['c'],activation='1767225600')
    def test_reconnect_redelivery_and_durable_before_ack(self):
        adapter=self.adapter(); adapter.connect()
        first=adapter.on_envelope(envelope())
        self.fake.callback(envelope()) # SDK reconnect redelivery
        self.assertEqual(len(self.fake.acks),2)
        self.assertEqual(len(self.svc.messages('s',caller=policy.Caller(role='orch',sources=frozenset({'s'}),identity='orch'))['messages']),1)
        self.assertEqual(adapter.health['status'],'connected')
        self.svc.con.execute("CREATE TRIGGER fail_ingress BEFORE INSERT ON channel_messages BEGIN SELECT RAISE(ABORT,'fake failure'); END")
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError): adapter.on_envelope(envelope('1767225601.1','failed'))
        self.assertEqual(len(self.fake.acks),2)
    def test_thread_pagination_and_checkpoints(self):
        adapter=self.adapter(); adapter.poll()
        self.assertEqual(self.fake.calls,[('c',None,None),('c','1767225600.000001',None),('c','1767225600.000001','next')])
        self.assertIsNotNone(self.svc.checkpoint('s','thread:c:1767225600.000001',caller=self.caller))
        adapter.poll()
        self.assertEqual(self.svc.con.execute('SELECT count(*) FROM channel_messages').fetchone()[0],3)
    def test_429_missing_scope_and_history_gap_preserve_cursor(self):
        adapter=self.adapter()
        for failure in (SlackError('ratelimited',retry_after=17),SlackError('missing_scope'),HistoryGap('gap')):
            self.fake.failure=failure
            with self.assertRaises(type(failure)): adapter.poll()
            self.assertIsNone(self.svc.checkpoint('s','history:c',caller=self.caller))
            self.assertEqual(adapter.health['status'],'degraded')
        self.assertTrue(adapter.health['retention_gap'])
    def test_delete_edit_and_late_redelivery(self):
        adapter=self.adapter(); first=adapter.on_envelope(envelope())
        changed=envelope(key='edit')
        changed['payload']['event']=dict(type='message',subtype='message_changed',channel='c',event_ts='1767225601.1',message=dict(ts='1767225600.000001',user='human',text='edited',edited={'ts':'1767225601.1'}))
        edited=adapter.on_envelope(changed)
        deleted=envelope(key='delete')
        deleted['payload']['event']=dict(type='message',subtype='message_deleted',channel='c',deleted_ts='1767225600.000001',previous_message={'user':'human'})
        adapter.on_envelope(deleted)
        reader=policy.Caller(role='orch',sources=frozenset({'s'}),identity='orch')
        for receipt in (first,edited): self.assertIsNone(self.svc.message(receipt['message_id'],caller=reader)['content']['text'])
        self.assertEqual(adapter.on_envelope(envelope(key='late'))['status'],'duplicate')
    def test_self_echo_allowlisted_bot_and_no_reply_zero_sends(self):
        adapter=self.adapter(); adapter.self_identities=frozenset({'self'})
        result=adapter.on_envelope(envelope(app_id='self',bot_id='bot'))
        self.assertEqual(result['loop_status'],'sent_echo')
        result=adapter.on_envelope(envelope('1767225602.1','proxy',app_id='allowed',bot_id='bot'))
        self.assertEqual(result['loop_status'],'unverified_automation')
        for kind in ('interim','final','no_reply'):
            with self.assertRaises(PermissionError): adapter.send(reply_kind=kind)
        self.assertEqual(self.fake.sent,[])
        self.assertEqual(self.svc.con.execute('SELECT count(*) FROM channel_processing_claims').fetchone()[0],0)
    def test_source_acl_and_tenant_rejection_do_not_ack(self):
        adapter=self.adapter(); payload=envelope(); payload['payload']['team_id']='other'
        with self.assertRaises(PermissionError): adapter.on_envelope(payload)
        adapter.caller=policy.Caller(role='adapter',sources=frozenset({'other'}),identity='s')
        with self.assertRaises(PermissionError): adapter.on_envelope(envelope())
        self.assertEqual(self.fake.acks,[])
    def test_socket_singleton_and_requested_sync_share_existing_connection(self):
        adapter=self.adapter(); adapter.connect(); self.addCleanup(adapter.close)
        other=SlackAdapter(self.svc,'s',self.caller,FakeSlack(),adapter.secret_store)
        self.assertEqual(other.connect()['status'],'busy')
        reader=policy.Caller(role='orch',sources=frozenset({'s'}),identity='orch')
        job=self.svc.request_sync('s','manual',{},caller=reader)
        adapter.run_pending()
        self.assertEqual(self.svc.sync_status(job['job_id'],caller=reader)['state'],'succeeded')
    def test_sdk_callback_can_persist_on_its_thread(self):
        import threading
        adapter=self.adapter(); adapter.connect(); self.addCleanup(adapter.close)
        errors=[]
        def callback():
            try: adapter.on_envelope(envelope())
            except Exception as error: errors.append(error)
        thread=threading.Thread(target=callback); thread.start(); thread.join()
        self.assertEqual(errors,[])
        self.assertEqual(self.fake.acks,['env-event'])
    def test_history_root_and_edited_message_share_socket_identity(self):
        adapter=self.adapter()
        first=adapter.on_envelope(envelope())
        adapter.poll()
        self.assertEqual(self.svc.con.execute('SELECT count(*) FROM channel_messages').fetchone()[0],3)
        raw=envelope()['payload']; raw['event']['edited']={'ts':'1767225610.000001'}; raw['event']['text']='edit'
        self.assertEqual(normalize_slack(raw)['revision_key'],'edit:1767225610.000001')
    def test_delete_with_previous_files_still_tombstones(self):
        adapter=self.adapter()
        deleted=envelope(key='delete-files')
        deleted['payload']['event']=dict(type='message',subtype='message_deleted',channel='c',deleted_ts='1767225600.000001',previous_message={'user':'human','files':[{'id':'synthetic-file'}]})
        receipt=adapter.on_envelope(deleted)
        self.assertIsNone(self.svc.message(receipt['message_id'],caller=self.caller)['content']['text'])

    def test_auth_identity_and_activation_bound(self):
        adapter=self.adapter()
        self.fake.auth_test=lambda credential: {'ok': True, 'team_id': 't', 'user_id': 'self', 'bot_id': 'bot'}
        adapter.connect(); self.addCleanup(adapter.close)
        payload=envelope(); payload['payload']['event']['user']='self'
        self.assertEqual(adapter.on_envelope(payload)['loop_status'], 'sent_echo')
        calls=[]
        self.fake.history=lambda conversation,oldest,end,**kwargs: calls.append((conversation,oldest)) or {'messages': []}
        adapter.poll()
        self.assertEqual(calls, [('c','1767225600')])

class SlackFileTest(ChannelCase):
    def test_file_share_metadata_and_text(self):
        payload=envelope(subtype='file_share', files=[dict(id='F1', name='photo.png', mimetype='image/png', size=4, duration=3, url_private='https://files.slack.com/files-pri/T/F1/photo.png')])['payload']
        item=normalize_slack(payload)['content']['attachments'][0]
        self.assertEqual(item['provider_attachment_ref'], 'F1')
        self.assertEqual(item['source_url_ref'], payload['event']['files'][0]['url_private'])
        self.assertEqual(item['duration'], 3)
        self.assertNotIn('token', str(item))
        self.assertEqual(normalize_slack(payload)['content']['text'], 'synthetic')
    def test_regular_message_files(self):
        payload=envelope(files=[dict(id='F',name='x.mp4',mimetype='video/mp4',size=4,url_private='https://files.slack.com/files-pri/T/F/x')])['payload']
        self.assertEqual(normalize_slack(payload)['content']['attachments'][0]['mime_type'],'video/mp4')

    def test_request_download_read_and_failures(self):
        from unittest.mock import patch
        adapter=SocketTest.adapter(self)
        reader=policy.Caller(role='orch',sources=frozenset({'s'}),identity='orch')
        for index, reason in enumerate((None,'missing_scope','download_failed','too_large','mime_not_allowed')):
            file=dict(id='F'+str(index),name='x.png',mimetype='image/png' if reason!='mime_not_allowed' else 'application/x-bad',size=4 if reason!='too_large' else 30*1024*1024,url_private='https://files.slack.com/files-pri/T/F/x')
            receipt=adapter.on_envelope(envelope(str(1767225600+index)+'.1','file'+str(index),files=[file]))
            aid=self.svc.message(receipt['message_id'],caller=reader)['content']['attachments'][0]['attachment_id']
            self.svc.request_attachment(aid,caller=reader)
            self.fake.get_attachment=lambda url: (_ for _ in ()).throw(SlackError(reason)) if reason in ('missing_scope','download_failed') else b'data'
            adapter.poll()
            result=self.svc.read_attachment(aid,caller=reader)
            if reason is None: self.assertEqual(result['content_base64'],'ZGF0YQ==')
            else:
                self.assertEqual(result['status'],'unavailable')
                self.assertEqual(self.svc.message(receipt['message_id'],caller=reader)['content']['attachments'][0]['note'],reason)

class SlackHttpTest(ChannelCase):
    def test_http_download_bounded_and_scope(self):
        from unittest.mock import patch
        from io import BytesIO
        from types import SimpleNamespace
        from orchd.channels.adapters.slack import SlackTransport, SlackError
        class Response(BytesIO):
            headers={'x-oauth-scopes':'files:read'}
        class Opener:
            def open(self, request, timeout):
                self.request=request
                return Response(b'data')
        opener=Opener(); transport=SlackTransport(); transport.web=SimpleNamespace(token='secret')
        with patch('urllib.request.build_opener',return_value=opener):
            self.assertEqual(transport.get_attachment('https://files.slack.com/files-pri/T/F/x'),b'data')
            self.assertEqual(opener.request.get_header('Authorization'),'Bearer secret')
            with self.assertRaises(SlackError): transport.get_attachment('https://example.com/x')
            Response.headers={'x-oauth-scopes':'channels:history'}
            with self.assertRaises(SlackError) as caught: transport.get_attachment('https://files.slack.com/files-pri/T/F/x')
            self.assertEqual(caught.exception.code,'missing_scope')

    def test_unsupported_diagnostic_only_type_and_subtype(self):
        adapter=SocketTest.adapter(self)
        payload=envelope(key='unsupported'); payload['payload']['event'].update(type='reaction_added',subtype='example',text='private')
        receipt=adapter.on_envelope(payload)
        content=self.svc.message(receipt['message_id'],caller=self.caller)['content']
        self.assertEqual(content['metadata']['diagnostic'],{'type':'reaction_added','subtype':'example'})
        self.assertNotIn('private',str(content))
    def test_request_through_fake_http_to_read(self):
        from unittest.mock import patch
        from io import BytesIO
        from types import SimpleNamespace
        from orchd.channels.adapters.slack import SlackTransport
        adapter=SocketTest.adapter(self)
        adapter.conversations=()
        http=SlackTransport(); http.web=SimpleNamespace(token='fake')
        self.fake.get_attachment=http.get_attachment
        class Response(BytesIO):
            headers={'x-oauth-scopes':'files:read'}
        class Opener:
            def open(self, request, timeout): return Response(b'data')
        reader=policy.Caller(role='orch',sources=frozenset({'s'}),identity='orch')
        file=dict(id='F',name='x.png',mimetype='image/png',size=4,url_private='https://files.slack.com/files-pri/T/F/x')
        receipt=adapter.on_envelope(envelope(files=[file]))
        aid=self.svc.message(receipt['message_id'],caller=reader)['content']['attachments'][0]['attachment_id']
        self.svc.request_attachment(aid,caller=reader)
        with patch('urllib.request.build_opener',return_value=Opener()): adapter.poll()
        self.assertEqual(self.svc.read_attachment(aid,caller=reader)['content_base64'],'ZGF0YQ==')

    def test_default_media_allowlist_and_reject_other(self):
        from orchd.channels.attachments import rejection_reason
        for mime in ('image/png','image/jpeg','image/gif','image/webp','image/heic','video/mp4','video/quicktime','video/webm','audio/mp4','audio/mpeg','audio/wav','audio/ogg','audio/webm'):
            with self.subTest(mime=mime): self.assertIsNone(rejection_reason(self.svc.channel_home,'s',mime,b'data'))
        self.assertEqual(rejection_reason(self.svc.channel_home,'s','application/x-bad',b'data'),'mime_rejected')
