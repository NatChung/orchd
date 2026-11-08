"""Exact outbound payloads and the transaction-free platform send boundary.

No provider retry is safe here: exceptions (including a missing receipt) mean
uncertain. Only the repository can claim a delivery or persist an outcome.
"""
import base64
import hashlib
import math
from .models import canonical, digest


def normalize_payload(payload, source, message, route, thread):
    allowed = {'draft_id', 'source_id', 'account', 'sender', 'target', 'body', 'expires_at',
               'thread', 'format', 'mentions', 'attachments', 'reply_kind', 'subject'}
    if not isinstance(payload, dict) or set(payload) - allowed:
        raise ValueError('invalid draft payload fields')
    for key in ('sender', 'body'):
        if not isinstance(payload.get(key), str) or not payload[key]:
            raise ValueError('sender and body required')
    if len(payload['body']) > 100000:
        raise ValueError('body exceeds limit')
    expires = payload.get('expires_at')
    if type(expires) not in (int, float) or not math.isfinite(expires) or expires < 0:
        raise ValueError('finite expires_at required')
    if payload.get('source_id', source['id']) != source['id'] or payload.get('account', source['account_ref']) != source['account_ref']:
        raise PermissionError('source or account mismatch')
    if payload['sender'] not in (source['id'], source['account_ref']):
        raise PermissionError('sender account mismatch')
    target = payload.get('target')
    if source['provider'] == 'gmail':
        if isinstance(target, str):
            target = {'to': [target], 'cc': [], 'bcc': []}
        if not isinstance(target, dict) or set(target) - {'to', 'cc', 'bcc'}:
            raise ValueError('email target requires To/Cc/Bcc')
        target = {k: target.get(k, []) for k in ('to', 'cc', 'bcc')}
        if not target['to'] or any(not isinstance(v, list) or not all(isinstance(a, str) and a and '\n' not in a and '\r' not in a for a in v) for v in target.values()):
            raise ValueError('invalid email recipients')
    elif not isinstance(target, str) or not target or target != message['external_ref']:
        raise PermissionError('target outside conversation route')
    reply_kind = payload.get('reply_kind', 'final')
    if reply_kind not in ('interim', 'final', 'no_reply'):
        raise ValueError('invalid reply kind')
    mentions = payload.get('mentions', [])
    if not isinstance(mentions, list) or len(mentions) > 100 or not all(isinstance(m, str) and m for m in mentions):
        raise ValueError('invalid mentions')
    attachments = payload.get('attachments', [])
    if not isinstance(attachments, list) or len(attachments) > 20:
        raise ValueError('invalid attachments')
    checked = []
    for item in attachments:
        if not isinstance(item, dict) or set(item) != {'name', 'bytes', 'sha256', 'data_base64'}:
            raise ValueError('attachment needs exact bytes and digest')
        if not isinstance(item['name'], str) or not item['name'] or any(c in item['name'] for c in '\r\n/\\'):
            raise ValueError('invalid attachment name')
        if type(item['bytes']) is not int or not 0 <= item['bytes'] <= 10_000_000:
            raise ValueError('invalid attachment size')
        if not isinstance(item['data_base64'], str) or len(item['data_base64']) > 13_333_336:
            raise ValueError('invalid attachment bytes')
        try:
            data = base64.b64decode(item['data_base64'], validate=True)
        except (ValueError, TypeError):
            raise ValueError('invalid attachment encoding') from None
        if len(data) != item['bytes'] or hashlib.sha256(data).hexdigest() != item['sha256']:
            raise ValueError('attachment digest or bytes mismatch')
        checked.append(dict(item, data_base64=base64.b64encode(data).decode('ascii')))
    fmt = payload.get('format', 'text')
    if fmt not in ('text', 'markdown'):
        raise ValueError('unsupported format')
    thread = payload.get('thread', thread)
    subject = payload.get('subject', '')
    if not isinstance(thread, str) or not isinstance(subject, str) or '\r' in subject or '\n' in subject:
        raise ValueError('invalid thread or subject')
    return dict(source_id=source['id'], provider=source['provider'], account=source['account_ref'],
                sender=payload['sender'], target=target, body=payload['body'], thread=thread, subject=subject,
                format=fmt, mentions=list(mentions), attachments=checked, reply_kind=reply_kind,
                route_id=route['id'] if route else None, route_version=route['version'] if route else None,
                authority_epoch=route['authority_epoch'] if route else source['authority_epoch'])


def preview(draft):
    """A byte-exact Entry question and an unambiguous affirmative answer."""
    import json
    value = json.loads(draft['payload_json'])
    # Binary bytes are bound by digest and byte count and shown as attachment metadata.
    shown = dict(value, attachments=[{k: v for k, v in a.items() if k != 'data_base64'} for a in value['attachments']])
    answer = f"approve {draft['draft_id']} v{draft['version']} {draft['payload_hash']}"
    text = ('Approve this exact outbound message?\n' + canonical(shown) +
            '\nExpires at: ' + str(draft['expires_at']) + '\nAnswer exactly:\n' + answer)
    return {'preview': text, 'answer': answer, 'payload_hash': draft['payload_hash']}


class OutboundRunner:
    """One sweep claims once; transports may reject more strictly, never authorize.

    The caller is a provisioned adapter and the injected transport owns all SDK
    and secret-store access. A real transport must disable its own HTTP retries.
    """
    def __init__(self, service, source_id, caller, transport):
        self.service, self.source_id, self.caller, self.transport = service, source_id, caller, transport

    def sweep(self):
        claims = self.service.claim_outbound(self.source_id, caller=self.caller)
        results = []
        for claim in claims:
            try:
                receipt = self.transport.send(claim['payload'], claim['idempotency_key'])
                if not isinstance(receipt, str) or not receipt:
                    raise ValueError('missing provider receipt')
            except Exception:
                # Never persist provider exception text: it can contain credentials/body.
                results.append(self.service.record_outcome(claim['id'], claim['attempt_no'], 'uncertain', None, caller=self.caller))
            else:
                # A failed DB write deliberately leaves claimed. Recovery records
                # uncertain; the runner must not put the platform call in a retry.
                results.append(self.service.record_outcome(claim['id'], claim['attempt_no'], 'provider_accepted', receipt, caller=self.caller))
        return results
