"""Operator source and route configuration helpers."""
import json
from . import policy, repository


def sources(svc, caller):
    policy.require_operator(caller)
    return [repository.source(svc.con, sid) for sid in sorted(repository.source_ids(svc.con))]


def set_enabled(svc, caller, source_id, enabled):
    policy.require_operator(caller)
    repository.set_source_enabled(svc.con, source_id, enabled)
    return {'id': source_id, 'enabled': enabled}


def change_route(svc, caller, action, value):
    existing = []
    for row in svc.routes(caller=caller):
        item = {k: row[k] for k in ('id', 'source_id', 'conversation_ref', 'owner_orch', 'project_ref', 'repo_ref', 'responder_source_id', 'mode', 'authority_epoch')}
        item['senders'] = json.loads(row['sender_scope_json'])
        existing.append(item)
    if action == 'add':
        if any(row['id'] == value.get('id') for row in existing): raise ValueError('route already exists')
        existing.append(value)
    else:
        if not any(row['id'] == value for row in existing): raise ValueError('unknown route')
        existing = [row for row in existing if row['id'] != value]
    return svc.apply_routes(existing, caller=caller)
