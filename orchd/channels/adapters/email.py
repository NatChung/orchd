"""Gmail read-only transport and message normalization."""
import base64
from datetime import datetime, timezone
from html.parser import HTMLParser

MAX_BODY_BYTES = 400_000
from ..attachments import MAX_ATTACHMENT_BYTES
READONLY_SCOPE = 'https://www.googleapis.com/auth/gmail.readonly'


class HistoryExpired(Exception):
    """The provider no longer retains the requested history cursor."""


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.hidden += 1
        elif tag in ('p', 'br', 'div', 'li'):
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in ('script', 'style') and self.hidden:
            self.hidden -= 1

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def _decode(value):
    if not isinstance(value, str) or len(value) > MAX_BODY_BYTES * 2:
        raise ValueError('message body exceeds limit')
    data = base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))
    if len(data) > MAX_BODY_BYTES:
        raise ValueError('message body exceeds limit')
    return data.decode('utf-8', errors='replace')


def normalize_message(message, *, tenant_ref):
    message_id, thread = message['id'], message['threadId']
    timestamp = datetime.fromtimestamp(int(message['internalDate']) / 1000, timezone.utc).isoformat()
    payload = message.get('payload')
    headers = {h['name'].lower(): h['value'] for h in (payload or {}).get('headers', [])}
    attachments, plain, html = [], [], []

    def visit(part):
        body = part.get('body') or {}
        filename = part.get('filename') or ''
        ref = body.get('attachmentId')
        size = body.get('size', 0)
        if filename:
            if len(attachments) >= 20:
                raise ValueError('too many attachments')
            item = {'name': filename, 'status': 'referenced' if ref and size <= MAX_ATTACHMENT_BYTES else 'unavailable', 'bytes': size}
            if ref:
                item['provider_attachment_ref'] = ref
            attachments.append(item)
        elif body.get('data') is not None:
            kind = part.get('mimeType', '').lower()
            if kind == 'text/plain':
                plain.append(_decode(body['data']))
            elif kind == 'text/html':
                parser = _Text()
                parser.feed(_decode(body['data']))
                html.append(''.join(parser.parts))
        for child in part.get('parts', []):
            visit(child)

    if payload:
        visit(payload)
    if plain or html:
        text = '\n'.join(plain or html)
        if len(text) > 100000:
            raise ValueError('message text exceeds limit')
        content = {'status':'complete', 'text':text, 'attachments':attachments}
    elif message.get('snippet'):
        content = {'status':'truncated', 'text':message['snippet'], 'attachments':attachments}
    else:
        content = {'status':'unavailable', 'text':None, 'unavailable_reason':'no_body', 'attachments':attachments}
    return {'schema_version':1, 'transport_event_key':message_id,
            'origin':{'provider':'gmail','tenant_ref':tenant_ref,'conversation_ref':thread,'message_id':message_id},
            'revision_key':'initial', 'conversation':{'kind':'email','thread_key':thread},
            'sender_ref':headers.get('from') or 'unknown', 'source_time':timestamp, 'content':content}

class EmailAdapter:
    """One source's receive-only polling adapter; service owns durable state."""
    def __init__(self, service, source_id, caller, transport, secret_store, *, activation, clock, overlap_seconds=60):
        self.service, self.source_id, self.caller = service, source_id, caller
        self.transport, self.secret_store = transport, secret_store
        self.activation, self.clock, self.overlap_seconds = activation, clock, overlap_seconds
        self.health = {'status':'offline', 'last_success':None, 'credential_error':False}

    def poll(self):
        return self._run(False)

    def run_pending(self):
        return self._run(True)

    def _run(self, pending):
        import uuid
        from .. import secret_store, sync
        holder = uuid.uuid4().hex
        generation = self.service.acquire_source_lease(self.source_id, holder, caller=self.caller, seconds=900)
        if generation is None:
            return {'status':'busy'}
        def platform_call(method, *args, **kwargs):
            self.service.renew_source_lease(self.source_id,holder,generation,caller=self.caller)
            return method(*args, **kwargs)
        job = None
        try:
            if pending:
                job = self.service.claim_sync_job(self.source_id, holder, generation, caller=self.caller)
                if job is None:
                    return {'status':'idle'}
            source = self.service.adapter_source(self.source_id, caller=self.caller)
            if source['provider'] != 'gmail':
                raise ValueError('Gmail source required')
            self.service.commit_checkpoint(self.source_id,'adapter_heartbeat',self.clock().isoformat(),[],holder,generation,caller=self.caller)
            try:
                credential = secret_store.resolve(source['credential_refs'].get('read'), adapter=self.caller.identity, source=self.source_id, provider=self.secret_store)
                if self.transport is None:
                    self.transport = GmailTransport(credential)
            except Exception:
                self.health.update(status='degraded', credential_error=True)
                raise
            for item in self.service.queued_attachments(self.source_id, caller=self.caller):
                data = platform_call(self.transport.get_attachment, item['provider_message_id'], item['provider_attachment_ref'])
                self.service.store_attachment(item['id'], data, holder, generation, caller=self.caller)
            previous = self.service.checkpoint(self.source_id, 'mail', caller=self.caller)
            cursor = datetime.fromisoformat(previous['committed_cursor']) if previous else None
            start, end = sync.fixed_window(cursor, self.activation, self.clock(), self.overlap_seconds)
            receipts, token, seen = [], None, set()
            history_start = None
            if hasattr(self.transport, 'current_history_id'):
                history_start = str(platform_call(self.transport.current_history_id))
                previous_history = self.service.checkpoint(self.source_id, 'history', caller=self.caller)
                if previous_history:
                    history_token, history_seen = None, set()
                    try:
                        for _ in range(1000):
                            page = platform_call(self.transport.list_history, previous_history['committed_cursor'], page_token=history_token)
                            for entry in page.get('history', []):
                                for added in entry.get('messagesAdded', []):
                                    raw = platform_call(self.transport.get_message, added['message']['id'])
                                    result = self.service.receive(self.source_id, normalize_message(raw, tenant_ref=source['tenant_ref']), caller=self.caller)
                                    if result['status'] not in ('received','duplicate'):
                                        raise ValueError('message receipt conflict')
                                    receipts.append(result['receipt_id'])
                            history_token = page.get('nextPageToken')
                            if not history_token:
                                break
                            if history_token in history_seen:
                                raise ValueError('repeated history page token')
                            history_seen.add(history_token)
                        else:
                            raise ValueError('history page limit exceeded')
                    except HistoryExpired:
                        start = self.activation
            for _ in range(1000):
                page = platform_call(self.transport.list_messages, start, end, page_token=token)
                for item in page.get('messages', []):
                    raw = platform_call(self.transport.get_message, item['id'])
                    result = self.service.receive(self.source_id, normalize_message(raw, tenant_ref=source['tenant_ref']), caller=self.caller)
                    if result['status'] not in ('received','duplicate'):
                        raise ValueError('message receipt conflict')
                    receipts.append(result['receipt_id'])
                token = page.get('nextPageToken')
                if not token:
                    break
                if token in seen:
                    raise ValueError('repeated page token')
                seen.add(token)
            else:
                raise ValueError('page limit exceeded')
            self.service.commit_checkpoint(self.source_id,'mail',end.isoformat(),receipts,holder,generation,caller=self.caller)
            if history_start is not None:
                self.service.commit_checkpoint(self.source_id,'history',history_start,receipts,holder,generation,caller=self.caller)
            if job:
                self.service.finish_sync_job(self.source_id, job, holder, generation, caller=self.caller)
            self.service.record_adapter_health(self.source_id,holder,generation,'healthy',caller=self.caller)
            self.health.update(status='healthy', last_success=end.isoformat(), credential_error=False)
            return {'status':'succeeded','count':len(receipts)}
        except Exception as error:
            if job:
                self.service.finish_sync_job(self.source_id, job, holder, generation, caller=self.caller, error='sync_failed')
            if self.health['status'] != 'degraded':
                self.health['status'] = 'degraded'
            status = getattr(error, 'status_code', None) or getattr(getattr(error, 'resp', None), 'status', None)
            if status in (401, 403):
                self.health['credential_error'] = True
            try:
                state = 'credential_error' if self.health['credential_error'] else 'degraded'
                self.service.record_adapter_health(self.source_id,holder,generation,state,caller=self.caller)
            except PermissionError:
                pass
            raise
        finally:
            self.service.release_source_lease(self.source_id,holder,generation,caller=self.caller)


class GmailTransport:
    """Real SDK is optional and is imported only when a transport is created."""
    def __init__(self, credential):
        import json
        import httplib2
        from google_auth_httplib2 import AuthorizedHttp
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        info = json.loads(credential.decode('utf-8'))
        credentials = Credentials.from_authorized_user_info(info, scopes=[READONLY_SCOPE])
        http = AuthorizedHttp(credentials, http=httplib2.Http(timeout=30))
        self._users = build('gmail','v1',http=http,cache_discovery=False).users()
        self._messages = self._users.messages()

    def current_history_id(self):
        return self._users.getProfile(userId='me').execute()['historyId']

    def list_history(self, cursor, page_token=None):
        from googleapiclient.errors import HttpError
        try:
            return self._users.history().list(userId='me',startHistoryId=cursor,historyTypes=['messageAdded'],maxResults=100,pageToken=page_token).execute()
        except HttpError as error:
            if error.resp.status == 404:
                raise HistoryExpired() from None
            raise

    def list_messages(self, start, end, page_token=None):
        query = f'after:{int(start.timestamp()) - 1} before:{int(end.timestamp())}'
        return self._messages.list(userId='me',q=query,maxResults=100,pageToken=page_token).execute()

    def get_message(self, message_id):
        return self._messages.get(userId='me',id=message_id,format='full').execute()

    def get_attachment(self, message_id, attachment_id):
        result = self._messages.attachments().get(userId='me',messageId=message_id,id=attachment_id).execute()
        data = result['data']
        if len(data) > (MAX_ATTACHMENT_BYTES * 4 // 3 + 8):
            raise ValueError('attachment exceeds limit')
        decoded = base64.urlsafe_b64decode(data + '=' * (-len(data) % 4))
        if len(decoded) > MAX_ATTACHMENT_BYTES:
            raise ValueError('attachment exceeds limit')
        return decoded
