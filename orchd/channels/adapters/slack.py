"""Slack Socket Mode and bounded history reconciliation, receive-only."""
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from .. import models
from ..loops import Automation
from ..secret_store import resolve

class HistoryGap(ValueError): pass

class SlackError(RuntimeError):
    def __init__(self, code, retry_after=0):
        super().__init__('Slack transport unavailable')
        self.code=code; self.retry_after=retry_after

def timestamp(value):
    value=Decimal(str(value))
    return (datetime.fromtimestamp(int(value),timezone.utc)+timedelta(microseconds=int((value%1)*1000000))).isoformat()

def normalize_slack(payload):
    raw=payload['event']; subtype=raw.get('subtype')
    if raw.get('type')!='message' or subtype not in (None,'bot_message','message_changed','message_deleted'):
        return None
    message=raw.get('message',raw) if subtype=='message_changed' else raw
    mid=raw['deleted_ts'] if subtype=='message_deleted' else message['ts']
    revision='edit:'+message['edited']['ts'] if message.get('edited',{}).get('ts') else 'initial'
    content={'status':'complete','text':message.get('text',''),'attachments':[]}
    if subtype=='message_changed': revision='edit:'+(message.get('edited',{}).get('ts') or raw.get('event_ts') or payload['event_id'])
    if subtype=='message_deleted':
        revision='deleted'
        content={'status':'unavailable','text':None,'attachments':[],'unavailable_reason':'deleted'}
        message=raw.get('previous_message',raw)
    if subtype!='message_deleted' and (message.get('files') or message.get('attachments')):
        content['status']='truncated'
    return models.validate_event(dict(schema_version=1,transport_event_key=payload['event_id'],
        origin=dict(provider='slack',tenant_ref=payload['team_id'],conversation_ref=raw['channel'],message_id=mid),
        revision_key=revision,conversation=dict(kind='channel',thread_key=message.get('thread_ts') or mid),
        sender_ref=message.get('user') or message.get('bot_id') or 'unknown',source_time=timestamp(mid),content=content))

class SlackAdapter:
    def __init__(self, service, source_id, caller, transport, secret_store, *, self_identities=(), conversations=(), activation='0', clock=None):
        import threading
        self._thread=threading.get_ident()
        self._session_factory=service.session_factory()
        self.service=service; self.source_id=source_id; self.caller=caller
        self.transport=transport; self.secret_store=secret_store
        self.self_identities=frozenset(self_identities); self.conversations=tuple(conversations)
        self.activation=activation; self.clock=clock or service.clock
        self._connection_lease=None
        self.health={'status':'offline','retention_gap':False,'credential_error':False}
    def _source(self):
        source=self.service.adapter_source(self.source_id,caller=self.caller)
        if source['provider']!='slack': raise ValueError('Slack source required')
        return source
    def _receive(self,payload):
        source=self._source()
        if payload.get('team_id')!=source['tenant_ref']: raise PermissionError('Slack tenant mismatch')
        event=normalize_slack(payload)
        if event is None:
            # Durable diagnostic only; unknown events never enter the processing lane.
            event=models.validate_event(dict(schema_version=1,transport_event_key=payload['event_id'],origin=dict(provider='slack',tenant_ref=source['tenant_ref'],conversation_ref=payload['event'].get('channel','diagnostic'),message_id='diagnostic:'+payload['event_id']),revision_key='initial',conversation=dict(kind='channel',thread_key=''),sender_ref='unknown',source_time=timestamp(payload.get('event_time',0)),content=dict(status='unavailable',text=None,attachments=[],unavailable_reason='unsupported_event')))
            context=Automation(app_ref='unsupported')
        else:
            raw=payload['event'].get('message',payload['event'])
            bot=raw.get('bot_id') or raw.get('app_id') or raw.get('subtype')=='bot_message'
            echo=event['sender_ref'] in self.self_identities or raw.get('app_id') in self.self_identities
            # Platform metadata never supplies verified causation or a budget reset.
            context=Automation(app_ref=raw.get('app_id') or raw.get('bot_id') or ('unknown-bot' if bot else ''),self_echo=echo)
        if self._connection_lease:
            holder,generation=self._connection_lease
            self.service.renew_source_lease(self.source_id,holder,generation,caller=self.caller,seconds=900)
        return self.service.receive(self.source_id,event,caller=self.caller,automation=context,accept_tombstone=True)
    def on_envelope(self,envelope):
        import threading
        if threading.get_ident()!=self._thread:
            session=self._session_factory()
            try:
                adapter=SlackAdapter(session,self.source_id,self.caller,self.transport,self.secret_store,self_identities=self.self_identities,clock=self.clock)
                adapter._connection_lease=self._connection_lease
                return adapter.on_envelope(envelope)
            finally:
                session.close()
        receipt=self._receive(envelope['payload'])
        if receipt['status'] not in ('received','duplicate'): raise ValueError('receipt conflict')
        self.transport.ack(envelope['envelope_id'])
        return receipt
    def connect(self):
        source=self._source()
        credentials={name:resolve(source['credential_refs'].get(name),adapter=self.caller.identity,source=self.source_id,provider=self.secret_store) for name in ('app','api')}
        import uuid
        if self._connection_lease: raise ValueError('Socket Mode already connected')
        holder='socket-'+uuid.uuid4().hex
        generation=self.service.acquire_source_lease(self.source_id,holder,caller=self.caller,seconds=900)
        if generation is None: return {'status':'busy'}
        self._connection_lease=(holder,generation)
        try:
            if self.transport is None: self.transport=SlackTransport()
            self.transport.connect(credentials,self.on_envelope)
        except Exception:
            self.close()
            raise
        # SDK owns reconnect; connection alone never marks history complete.
        self.health['status']='connected'
    def poll(self):
        return self._run(False)
    def run_pending(self):
        return self._run(True)
    def _run(self,pending):
        import uuid
        source=self._source(); holder='slack-'+uuid.uuid4().hex
        lease=self.service.acquire_source_lease(self.source_id,holder,caller=self.caller,seconds=900) if not self._connection_lease else self._connection_lease[1]
        if lease is None: return {'status':'busy'}
        generation=lease
        if self._connection_lease: holder,generation=self._connection_lease
        job=None
        try:
            self.service.renew_source_lease(self.source_id,holder,generation,caller=self.caller,seconds=900)
            if pending:
                job=self.service.claim_sync_job(self.source_id,holder,generation,caller=self.caller)
                if job is None: return {'status':'idle'}
            credential=resolve(source['credential_refs'].get('api'),adapter=self.caller.identity,source=self.source_id,provider=self.secret_store)
            if self.transport is None:
                self.transport=SlackTransport()
                self.transport.configure(credential)
            for conversation in self.conversations:
                self._history(source,conversation,None,holder,generation)
            if job: self.service.finish_sync_job(self.source_id,job,holder,generation,caller=self.caller)
            self.service.commit_checkpoint(self.source_id,'adapter_heartbeat',self.clock().isoformat(),[],holder,generation,caller=self.caller)
            self.health.update(status='healthy',retention_gap=False)
            self.service.record_adapter_health(self.source_id,holder,generation,'healthy',caller=self.caller)
            return {'status':'succeeded'}
        except Exception as error:
            if job:
                try:
                    self.service.finish_sync_job(self.source_id,job,holder,generation,caller=self.caller,error='sync_failed')
                except (PermissionError, ValueError):
                    pass
            self.health.update(status='degraded',retention_gap=isinstance(error,HistoryGap),credential_error=getattr(error,'code',None)=='missing_scope')
            try:
                self.service.record_adapter_health(self.source_id,holder,generation,'degraded',caller=self.caller)
            except PermissionError:
                pass
            raise
        finally:
            if not self._connection_lease:
                import sys
                failed = sys.exc_info()[0] is not None
                try:
                    self.service.release_source_lease(self.source_id,holder,generation,caller=self.caller)
                except PermissionError:
                    if not failed:
                        raise
    def _history(self,source,conversation,thread,holder,generation):
        scope='thread:'+conversation+':'+thread if thread else 'history:'+conversation
        previous=self.service.checkpoint(self.source_id,scope,caller=self.caller)
        oldest=previous['committed_cursor'] if previous else self.activation
        oldest=str(max(Decimal('0'),Decimal(oldest)-Decimal('300')))
        end=str(int(self.clock().timestamp()))
        token=None; seen=set(); receipts=[]; threads=set()
        for _ in range(1000):
            self.service.renew_source_lease(self.source_id,holder,generation,caller=self.caller)
            page=self.transport.history(conversation,oldest,end,thread=thread,cursor=token)
            if page.get('gap'): raise HistoryGap('history unavailable')
            for raw in page.get('messages',[]):
                event=dict(raw,channel=conversation,type='message')
                if thread: event['thread_ts']=thread
                key='history:'+models.digest({'conversation':conversation,'message':raw})
                result=self._receive(dict(team_id=source['tenant_ref'],event_id=key,event=event))
                if result['status'] not in ('received','duplicate'): raise ValueError('history receipt conflict')
                receipts.append(result['receipt_id'])
                if not thread and raw.get('reply_count',0): threads.add(raw['ts'])
            token=page.get('response_metadata',{}).get('next_cursor')
            if not token:
                if page.get('has_more'): raise HistoryGap('history pagination incomplete')
                break
            if token in seen: raise HistoryGap('repeated history cursor')
            seen.add(token)
        else: raise HistoryGap('history page limit')
        for root in sorted(threads): self._history(source,conversation,root,holder,generation)
        self.service.commit_checkpoint(self.source_id,scope,end,receipts,holder,generation,caller=self.caller)
    def close(self):
        if self._connection_lease:
            holder,generation=self._connection_lease
            self._connection_lease=None
            try:
                if self.transport is not None and hasattr(self.transport,'close'): self.transport.close()
            finally:
                self.service.release_source_lease(self.source_id,holder,generation,caller=self.caller)
    def send(self, *, transport_factory=None, **payload):
        from .base import OutboundAdapter
        if payload:
            raise PermissionError('raw payload cannot bypass outbound gate')
        if transport_factory is None:
            raise PermissionError('outbound transport not enabled')
        return OutboundAdapter(self.service,self.source_id,self.caller,self.secret_store,transport_factory).sweep()

class SlackTransport:
    """Optional SDK, created only by the adapter. SDK automatically reconnects."""
    def configure(self, credential):
        from slack_sdk.web import WebClient
        self.web=WebClient(token=credential.decode(),timeout=30)
    def connect(self, credentials, callback):
        from slack_sdk.socket_mode import SocketModeClient
        self.configure(credentials['api'])
        self.socket=SocketModeClient(app_token=credentials['app'].decode(),web_client=self.web)
        def listener(client,request):
            if request.type=='events_api': callback({'envelope_id':request.envelope_id,'payload':request.payload})
        self.socket.socket_mode_request_listeners.append(listener)
        self.socket.connect()
    def ack(self,envelope_id):
        from slack_sdk.socket_mode.response import SocketModeResponse
        self.socket.send_socket_mode_response(SocketModeResponse(envelope_id=envelope_id))
    def history(self, conversation, oldest, latest, *, thread=None,cursor=None):
        try:
            args=dict(channel=conversation,oldest=oldest,latest=latest,inclusive=True,limit=100,cursor=cursor)
            return self.web.conversations_replies(ts=thread,**args) if thread else self.web.conversations_history(**args)
        except Exception as error:
            response=getattr(error,'response',None)
            if response is None: raise SlackError('transport_error') from None
            raise SlackError(response.get('error','transport_error'),getattr(response,'headers',{}).get('Retry-After',0)) from None
    def close(self):
        if hasattr(self,'socket'): self.socket.close()


class SlackSendTransport:
    """One Web API call, with SDK HTTP retries disabled by the client factory.

    PR5 supports text/markdown. File upload is a separate external effect and
    fails closed here until a separately reviewed multi-step send protocol exists.
    """
    def __init__(self, client, account):
        self.client, self.account = client, account

    def send(self, payload, idempotency_key):
        if payload['provider'] != 'slack' or payload['account'] != self.account:
            raise PermissionError('Slack sender mismatch')
        if payload['attachments']:
            raise ValueError('Slack file sending unavailable')
        # Mentions are permitted only when explicitly bound by the approval.
        import re
        actual = re.findall(r'<@([^>]+)>', payload['body'])
        if set(actual) != set(payload['mentions']) or '<!channel' in payload['body'] or '<!here' in payload['body'] or '<!everyone' in payload['body']:
            raise ValueError('Slack mentions differ from approval')
        import uuid
        result = self.client.chat_postMessage(channel=payload['target'], text=payload['body'],
            thread_ts=payload['thread'] or None, client_msg_id=str(uuid.uuid5(uuid.NAMESPACE_URL,idempotency_key)),
            parse='none', link_names=False, mrkdwn=payload['format']=='markdown',
            unfurl_links=False, unfurl_media=False)
        if not result.get('ok') or not result.get('ts'):
            raise ValueError('Slack acceptance receipt missing')
        return str(result['ts'])

    @classmethod
    def from_credential(cls, material, account):
        from slack_sdk.web import WebClient
        return cls(WebClient(token=material.decode(), timeout=30, retry_handlers=[]), account)
