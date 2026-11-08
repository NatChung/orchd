"""LINE direct webhook ingress. Only signed raw bytes enter the private spool."""
import base64
import hashlib
import hmac
import json
from datetime import datetime, timezone
from .. import models
from ..secret_store import resolve

MAX_WEBHOOK_BYTES=1024*1024


class LineSendTransport:
    """Push only: an approval never consumes an inbound short-lived reply token.

    The injected API boundary is push_message(target, messages, retry_key),
    returning the provider's durable request receipt. No implicit SDK retries.
    """
    def __init__(self, client, account):
        self.client, self.account = client, account

    def send(self, payload, idempotency_key):
        if payload['provider'] != 'line' or payload['account'] != self.account:
            raise PermissionError('LINE sender mismatch')
        if payload['attachments'] or payload['mentions'] or payload['thread'] or payload['format'] != 'text' or len(payload['body']) > 5000:
            raise ValueError('unsupported LINE push payload')
        import uuid
        result = self.client.push_message(payload['target'], [{'type':'text','text':payload['body']}], str(uuid.uuid5(uuid.NAMESPACE_URL,idempotency_key)))
        receipt = result.get('request_id') if isinstance(result, dict) else None
        if not isinstance(receipt, str) or not receipt:
            raise ValueError('LINE acceptance receipt missing')
        return receipt

    @classmethod
    def from_credential(cls, material, account):
        class PushClient:
            def push_message(self, target, messages, retry_key):
                # urllib performs one request; never install an automatic retry
                # handler for this non-idempotent boundary.
                from urllib.request import Request, urlopen
                request = Request('https://api.line.me/v2/bot/message/push',
                    data=json.dumps({'to':target,'messages':messages},ensure_ascii=False).encode('utf-8'),
                    headers={'Authorization':'Bearer '+material.decode(), 'Content-Type':'application/json',
                             'X-Line-Retry-Key':retry_key},method='POST')
                with urlopen(request,timeout=30) as response:
                    return {'request_id':response.headers.get('x-line-request-id')}
        return cls(PushClient(),account)

def valid_line_signature(raw, signature_header, signing_key):
    if not isinstance(raw,bytes) or not isinstance(signature_header,str) or not signing_key:
        return False
    expected=base64.b64encode(hmac.new(signing_key,raw,hashlib.sha256).digest()).decode('ascii')
    try:
        return hmac.compare_digest(expected,signature_header)
    except TypeError:
        return False

def normalize_line(event, tenant_ref):
    source=event['source']; kind=event['type']
    if kind not in ('message','unsend'):
        # Non-message lifecycle events do not create a processing claim.
        return None
    conversation=source.get('groupId') or source.get('roomId') or source['userId']
    mid=event['unsend']['messageId'] if kind=='unsend' else event['message']['id']
    message=event.get('message',{})
    content={'status':'complete','text':message.get('text',''),'attachments':[]}
    if kind=='unsend':
        content={'status':'unavailable','text':None,'attachments':[],'unavailable_reason':'deleted'}
    elif message.get('type')!='text':
        content={'status':'unavailable','text':None,'attachments':[],'unavailable_reason':'unsupported_message_type'}
    return models.validate_event(dict(schema_version=1,transport_event_key=event['webhookEventId'],
        origin=dict(provider='line',tenant_ref=tenant_ref,conversation_ref=conversation,message_id=mid),
        revision_key='deleted' if kind=='unsend' else 'initial',
        conversation=dict(kind='dm' if source['type']=='user' else 'group',thread_key=''),
        sender_ref=source.get('userId','unknown'),
        source_time=datetime.fromtimestamp(event['timestamp']/1000,timezone.utc).isoformat(),content=content))

class LineReceiver:
    def __init__(self, service, source_id, caller, spool, secret_store):
        if spool.source_id!=source_id: raise PermissionError('spool source mismatch')
        self.service=service; self.source_id=source_id; self.caller=caller
        self.spool=spool; self.secret_store=secret_store
        self.health={'retention_gap':False,'status':'offline'}
    def receive(self, raw, signature):
        if not isinstance(raw,bytes) or len(raw)>MAX_WEBHOOK_BYTES:
            return 413
        source=self.service.adapter_source(self.source_id,caller=self.caller)
        if source['provider']!='line': raise ValueError('LINE source required')
        try:
            key=resolve(source['credential_refs'].get('signing'),adapter=self.caller.identity,source=self.source_id,provider=self.secret_store)
        except ValueError:
            return 503
        if not valid_line_signature(raw,signature,key): return 403
        try:
            payload=json.loads(raw)
            if not isinstance(payload,dict) or not isinstance(payload.get('events'),list): return 400
            events=[]
            for item in payload['events']:
                event=normalize_line(item,source['tenant_ref'])
                if event is not None: events.append(event)
            self.spool.append(events)
            # Unsend is a privacy operation: make content unavailable before success,
            # independently of the asynchronous normal-message drain.
            for event in events:
                if event['content'].get('unavailable_reason') == 'deleted':
                    self.service.receive(self.source_id,event,caller=self.caller,accept_tombstone=True)
        except (KeyError,TypeError,OverflowError):
            return 400
        except ValueError as error:
            if str(error).startswith('retention_gap:'):
                self.health.update(retention_gap=True,status='degraded')
                return 503
            return 409 if 'conflict' in str(error) else 400
        except OSError:
            self.health['status']='degraded'
            return 503
        self.health['status']='healthy'
        return 200
    def drain(self, *, limit=100):
        import uuid
        holder='line-'+uuid.uuid4().hex
        generation=self.service.acquire_source_lease(self.source_id,holder,caller=self.caller,seconds=900)
        if generation is None: return 0
        count=0
        try:
            for item in self.spool.pending(limit=limit):
                self.service.renew_source_lease(self.source_id,holder,generation,caller=self.caller)
                receipt=self.service.receive(self.source_id,item['event'],caller=self.caller,accept_tombstone=True)
                if receipt['status'] not in ('received','duplicate'): raise ValueError('receipt conflict')
                self.spool.ack(item['seq']); count+=1
            return count
        finally:
            self.service.release_source_lease(self.source_id,holder,generation,caller=self.caller)
    def send(self, *, transport_factory=None, **payload):
        from .base import OutboundAdapter
        if payload:
            raise PermissionError('raw payload cannot bypass outbound gate')
        if transport_factory is None:
            raise PermissionError('outbound transport not enabled')
        return OutboundAdapter(self.service,self.source_id,self.caller,self.secret_store,transport_factory).sweep()
    def handler(self):
        """A dedicated HTTP surface: no operator/admin routes, no request logging."""
        from http.server import BaseHTTPRequestHandler
        receiver=self
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                if self.path!='/webhook': self.send_error(404); return
                try: size=int(self.headers.get('Content-Length','-1'))
                except ValueError: size=-1
                if not 0<=size<=MAX_WEBHOOK_BYTES:
                    self.send_error(413); return
                self.connection.settimeout(10)
                raw=self.rfile.read(size)
                if len(raw)!=size: self.send_error(400); return
                status=receiver.receive(raw,self.headers.get('X-Line-Signature'))
                self.send_response(status); self.end_headers()
            def log_message(self,*args): pass
        return Handler
