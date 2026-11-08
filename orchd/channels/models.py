"""Normalized channel values and wire limits."""
import hashlib
import json
from datetime import datetime, timezone


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def validate_event(event):
    allowed = {'schema_version','transport_event_key','origin','revision_key','conversation','sender_ref','source_time','content'}
    if not isinstance(event, dict) or not allowed <= set(event) or set(event) - (allowed | {'suggestion', 'target'}) or type(event['schema_version']) is not int or event['schema_version'] != 1:
        raise ValueError('invalid event fields or schema version')
    if 'suggestion' in event:
        from orchd import intake_bridge
        intake_bridge.normalize_suggestion(event['suggestion'])
    origin, conversation, content = event['origin'], event['conversation'], event['content']
    if not isinstance(origin, dict) or set(origin) != {'provider','tenant_ref','conversation_ref','message_id'}:
        raise ValueError('invalid origin')
    if not isinstance(conversation, dict) or set(conversation) != {'kind','thread_key'}:
        raise ValueError('invalid conversation')
    if conversation['kind'] not in ('channel', 'group', 'dm', 'email', 'peer') or not isinstance(conversation['thread_key'], str):
        raise ValueError('invalid conversation')
    if not isinstance(origin['tenant_ref'], str):
        raise ValueError('invalid tenant')
    if not isinstance(content, dict) or set(content) - {'status','text','attachments','unavailable_reason','mentions','format'}:
        raise ValueError('invalid content')
    metadata = {'mentions': content.get('mentions', []), 'format': content.get('format'), 'target': event.get('target')}
    if not isinstance(metadata['mentions'], list) or not all(isinstance(m, str) for m in metadata['mentions']):
        raise ValueError('invalid mentions')
    if metadata['format'] is not None and not isinstance(metadata['format'], str):
        raise ValueError('invalid format')
    if metadata['target'] is not None and not isinstance(metadata['target'], dict):
        raise ValueError('invalid target')
    try:
        metadata_bytes = canonical(metadata).encode('utf-8')
    except (TypeError, ValueError):
        raise ValueError('invalid content metadata') from None
    if len(metadata_bytes) > 20000:
        raise ValueError('content metadata exceeds limit')
    if content.get('status') not in ('complete','truncated','unavailable'):
        raise ValueError('invalid content status')
    if content['status'] == 'unavailable':
        if content.get('text') is not None:
            raise ValueError('unavailable content cannot have text')
        if not isinstance(content.get('unavailable_reason'), str) or not content['unavailable_reason']:
            raise ValueError('unavailable reason required')
    elif not isinstance(content.get('text'), str) or len(content['text']) > 100000:
        raise ValueError('invalid text')
    if not isinstance(content.get('attachments', []), list) or len(content.get('attachments', [])) > 20:
        raise ValueError('invalid attachments')
    for item in content.get('attachments', []):
        if not isinstance(item, dict) or set(item) - {'name', 'status', 'sha256', 'bytes', 'note', 'provider_attachment_ref'}:
            raise ValueError('invalid attachment')
        if not isinstance(item.get('name'), str) or not item['name'] or item.get('status') not in ('referenced', 'unavailable'):
            raise ValueError('invalid attachment metadata')
        if 'provider_attachment_ref' in item and (not isinstance(item['provider_attachment_ref'], str) or not item['provider_attachment_ref']):
            raise ValueError('invalid attachment reference')
        if 'bytes' in item and (type(item['bytes']) is not int or item['bytes'] < 0):
            raise ValueError('invalid attachment size')
        if 'sha256' in item:
            import re
            if not isinstance(item['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', item['sha256']):
                raise ValueError('invalid attachment digest')
    for value in [event['transport_event_key'], event['revision_key'], event['sender_ref'], origin['provider'], origin['conversation_ref'], origin['message_id']]:
        if not isinstance(value, str) or not value:
            raise ValueError('invalid identifier')
    try:
        dt = datetime.fromisoformat(event['source_time'].replace('Z','+00:00'))
        if dt.utcoffset() is None:
            raise ValueError()
    except (ValueError, AttributeError):
        raise ValueError('source_time must have timezone') from None
    normalized = dict(event)
    normalized['source_time'] = dt.astimezone(timezone.utc).isoformat()
    normalized['content'] = dict(content)
    normalized['content'].setdefault('text', None)
    normalized['content'].setdefault('attachments', [])
    return normalized
