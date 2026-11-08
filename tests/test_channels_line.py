import base64
import hashlib
import hmac
from channel_helpers import ChannelCase
from orchd.channels.adapters.line import valid_line_signature

class LineTest(ChannelCase):
    def test_signature_uses_exact_unicode_and_newline_bytes(self):
        raw='{"events": [], "text": "合成\\n"}\n'.encode()
        signature=base64.b64encode(hmac.new(b'fake',raw,hashlib.sha256).digest()).decode()
        self.assertTrue(valid_line_signature(raw,signature,b'fake'))
        for bad in (None,'wrong'):
            self.assertFalse(valid_line_signature(raw,bad,b'fake'))
        self.assertFalse(valid_line_signature(raw.rstrip(),signature,b'fake'))
import json
from pathlib import Path
from orchd.channels.spool import Spool
from orchd.channels.adapters.line import LineReceiver
from orchd.channels.secret_store import FakeSecretStore
from orchd.channels import policy

def message(key='e',mid='m',timestamp=1767225600000):
    return dict(type='message',webhookEventId=key,timestamp=timestamp,source=dict(type='group',groupId='c',userId='human'),message=dict(type='text',id=mid,text='合成\ntext'))

class ReceiverTest(ChannelCase):
    def receiver(self):
        self.caller=self.source(provider='line')
        self.spool=Spool(Path(self.tmp.name)/'spool','s'); self.addCleanup(self.spool.close)
        secrets=FakeSecretStore({('s','s','os-store:line/signing'):b'fake'})
        return LineReceiver(self.svc,'s',self.caller,self.spool,secrets)
    def request(self,receiver,events):
        raw=json.dumps({'events':events},ensure_ascii=False,indent=2).encode()
        signature=base64.b64encode(hmac.new(b'fake',raw,hashlib.sha256).digest()).decode()
        return receiver.receive(raw,signature)
    def test_bad_and_missing_signature_leave_no_durable_event(self):
        receiver=self.receiver()
        for signature in (None,'wrong','非ascii'):
            self.assertEqual(receiver.receive(b'{"events": []}',signature),403)
        self.assertEqual(self.spool.pending(),[])
        self.assertEqual(self.svc.messages('s',caller=self.caller)['messages'],[])
    def test_empty_multi_event_and_out_of_order_redelivery(self):
        receiver=self.receiver()
        self.assertEqual(self.request(receiver,[]),200)
        self.assertEqual(self.request(receiver,[message('later','b',1767225602000),message('earlier','a')]),200)
        receiver.drain()
        replay=message('earlier','a'); replay['deliveryContext']={'isRedelivery':True}
        self.assertEqual(self.request(receiver,[replay]),200)
        receiver.drain()
        reader=policy.Caller(role='orch',sources=frozenset({'s'}),identity='orch')
        rows=self.svc.messages('s',caller=reader)['messages']
        self.assertEqual(len(rows),2)
        self.assertEqual(rows[0]['content']['text'],'合成\ntext')
    def test_unsend_before_message_never_revives_body(self):
        receiver=self.receiver()
        unsend=dict(type='unsend',webhookEventId='u',timestamp=1767225603000,source=dict(type='group',groupId='c',userId='human'),unsend={'messageId':'m'})
        self.assertEqual(self.request(receiver,[unsend,message()]),200)
        receiver.drain()
        rows=self.svc.messages('s',caller=self.caller)['messages']
        self.assertTrue(rows)
        self.assertTrue(all(row['content']['text'] is None for row in rows))
        self.assertEqual(self.request(receiver,[message('late')]),200)
        receiver.drain()
        self.assertTrue(all(row['content']['text'] is None for row in self.svc.messages('s',caller=self.caller)['messages']))
    def test_unsend_after_message_clears_original_immediately(self):
        receiver=self.receiver(); self.request(receiver,[message()]); receiver.drain()
        unsend=dict(type='unsend',webhookEventId='u',timestamp=1767225603000,source=dict(type='group',groupId='c',userId='human'),unsend={'messageId':'m'})
        self.assertEqual(self.request(receiver,[unsend]),200)
        reader=policy.Caller(role='orch',sources=frozenset({'s'}),identity='orch')
        self.assertTrue(all(row['content']['text'] is None for row in self.svc.messages('s',caller=reader)['messages']))
    def test_lost_ack_replays_after_restart(self):
        receiver=self.receiver(); self.request(receiver,[message()])
        from unittest.mock import patch
        original=self.spool.ack
        with patch.object(self.spool,'ack',side_effect=OSError('lost ack')):
            with self.assertRaises(OSError): receiver.drain()
        self.svc.close(); self.open(); receiver.service=self.svc
        receiver.drain()
        self.assertEqual(self.spool.pending(),[])
        self.assertEqual(self.svc.con.execute('SELECT count(*) FROM channel_messages').fetchone()[0],1)
    def test_invalid_batch_is_not_partially_persisted_and_no_reply_sends_nothing(self):
        receiver=self.receiver()
        self.assertEqual(self.request(receiver,[message(),{'type':'message'}]),400)
        self.assertEqual(self.spool.pending(),[])
        for kind in ('interim','final','no_reply'):
            with self.assertRaises(PermissionError): receiver.send(reply_kind=kind)
        receiver.secret_store=FakeSecretStore({})
        self.assertEqual(receiver.receive(b'{"events":[]}',None),503)
    def test_http_surface_only_accepts_webhook_and_preserves_raw_bytes(self):
        import io
        receiver=self.receiver(); Handler=receiver.handler()
        raw=json.dumps({'events':[message()]},ensure_ascii=False).encode()+b'\n'
        signature=base64.b64encode(hmac.new(b'fake',raw,hashlib.sha256).digest()).decode()
        handler=object.__new__(Handler)
        handler.path='/webhook'; handler.headers={'Content-Length':str(len(raw)),'X-Line-Signature':signature}
        handler.rfile=io.BytesIO(raw)
        class FakeConnection:
            def settimeout(self,value): pass
        handler.connection=FakeConnection(); results=[]
        handler.send_response=results.append; handler.send_error=results.append; handler.end_headers=lambda:None
        handler.do_POST(); self.assertEqual(results,[200])
        handler.path='/operator'; handler.do_POST(); self.assertEqual(results,[200,404])
        self.assertFalse(hasattr(Handler,'do_GET'))
    def test_capacity_gap_and_source_acl_fail_closed(self):
        receiver=self.receiver(); self.spool.max_events=1
        self.assertEqual(self.request(receiver,[message('a','a')]),200)
        self.assertEqual(self.request(receiver,[message('b','b')]),503)
        self.assertTrue(receiver.health['retention_gap'])
        receiver.caller=policy.Caller(role='adapter',sources=frozenset({'other'}),identity='s')
        with self.assertRaises(PermissionError): self.request(receiver,[])
