"""Channel storage, with foreign keys isolated from the legacy store connection."""
import sqlite3
from importlib.resources import files

from orchd import store


def _statements():
    sql = '\n'.join(files('orchd.channels').joinpath('migrations/' + name).read_text(encoding='utf-8')
                    for name in ('0001.sql', '0002.sql', '0003.sql'))
    statement = ''
    for line in sql.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            if not statement.lstrip().upper().startswith('PRAGMA'):
                yield statement
            statement = ''
    if statement.strip():
        raise ValueError('incomplete channel migration')


def schema_plan(path):
    """Validate existing channel objects on a private copy, never migrate in place."""
    from pathlib import Path
    from orchd.stats import open_snapshot
    path = Path(path)
    result = dict(version=3, status='pending', rollback='retain additive tables')
    if not path.is_file():
        return result
    expected = sqlite3.connect(':memory:')
    try:
        for statement in _statements():
            expected.execute(statement)
        objects = expected.execute("SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL AND name LIKE 'channel_%'").fetchall()
    finally:
        expected.close()
    with open_snapshot(path) as snap:
        for kind, name, sql in objects:
            actual = snap.con.execute('SELECT sql FROM sqlite_master WHERE type=? AND name=?', (kind,name)).fetchone()
            if actual and actual[0] != sql:
                raise ValueError('incompatible channel schema: ' + name)
        if snap.con.execute("SELECT 1 FROM sqlite_master WHERE name='orchd_channel_migrations'").fetchone():
            versions = {r[0] for r in snap.con.execute('SELECT version FROM orchd_channel_migrations')}
            if versions - {1,2,3}:
                raise ValueError('unsupported channel migration version')
            if 3 in versions:
                result['status'] = 'current'
    return result


def connect(path=None):
    legacy = store.connect(path)
    try:
        dbpath = legacy.execute('PRAGMA database_list').fetchone()[2]
    finally:
        legacy.close()
    con = sqlite3.connect(dbpath, timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys=ON')
    if con.execute('PRAGMA foreign_keys').fetchone()[0] != 1:
        con.close()
        raise RuntimeError('channel foreign keys unavailable')
    try:
        # Compare the canonical SQLite schema, including indexes and checks, on every open.
        expected = sqlite3.connect(':memory:')
        try:
            for statement in _statements():
                expected.execute(statement)
            objects = expected.execute("SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL AND name LIKE 'channel_%' ORDER BY type,name").fetchall()
        finally:
            expected.close()
        with store.immediate(con):
            con.execute('CREATE TABLE IF NOT EXISTS orchd_channel_migrations(version INTEGER PRIMARY KEY)')
            versions = {row[0] for row in con.execute('SELECT version FROM orchd_channel_migrations')}
            if versions - {1, 2, 3}:
                raise ValueError('unsupported channel migration version')
            for statement in _statements():
                con.execute(statement)
            for kind, name, sql in objects:
                actual = con.execute('SELECT sql FROM sqlite_master WHERE type=? AND name=?', (kind, name)).fetchone()
                if actual is None or actual[0] != sql:
                    raise ValueError(f'incompatible channel schema: {name}')
            con.execute('INSERT OR IGNORE INTO orchd_channel_migrations VALUES (1)')
            con.execute('INSERT OR IGNORE INTO orchd_channel_migrations VALUES (2)')
            con.execute('INSERT OR IGNORE INTO orchd_channel_migrations VALUES (3)')
        return con
    except BaseException:
        con.close()
        raise


def provision(con, source):
    from datetime import datetime, timezone
    from .models import canonical, digest
    now = datetime.now(timezone.utc).isoformat()
    with store.immediate(con):
        con.execute('INSERT INTO channel_sources(id,host_id,provider,tenant_ref,account_ref,adapter_id,credential_refs_json,enabled,authority_epoch,manifest_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                    (source['id'], source['host_id'], source['provider'], source.get('tenant_ref',''), source['account_ref'], source['adapter_id'], canonical(source.get('credential_refs',{})), int(source.get('enabled',False)), 1, digest(source), now))


def outbound_draft(con, draft_id, version):
    from .models import digest
    import json
    if not isinstance(draft_id, str) or type(version) is not int or version < 1:
        raise ValueError('invalid draft identity')
    row = con.execute('SELECT * FROM intake_drafts WHERE draft_id=? AND version=?', (draft_id, version)).fetchone()
    if row is None or not row['payload_json']:
        raise ValueError('exact channel draft required; legacy body approval cannot authorize it')
    value = json.loads(row['payload_json'])
    if digest(value) != row['payload_hash']:
        raise ValueError('draft payload hash changed')
    # Detect mutations of either the legacy fields or the full payload.
    from orchd import intake
    from .delivery import normalize_payload
    message = con.execute('SELECT id FROM channel_messages WHERE intake_receipt_id=?', (row['receipt_id'],)).fetchone()
    if message is None:
        raise ValueError('draft message missing')
    message = get_message(con, message[0])
    src = source(con, value['source_id'])
    if message['source_id'] != src['id'] or message['deleted_at']:
        raise ValueError('draft source changed or message deleted')
    route = con.execute('SELECT * FROM channel_routes WHERE id=? AND version=?', (value['route_id'], value['route_version'])).fetchone()
    normalized = normalize_payload({k: v for k, v in value.items() if k not in ('provider', 'route_id', 'route_version', 'authority_epoch')}
                                   | {'expires_at': row['expires_at']}, src, message, route, value['thread'])
    target = json.dumps(value['target'], sort_keys=True) if isinstance(value['target'], dict) else value['target']
    if (normalized != value or row['sender'] != value['sender'] or row['target'] != target or
            row['source_ref'] != message['external_ref'] or row['body'] != value['body'] or intake._sha(row['body']) != row['body_sha256']):
        raise ValueError('draft fields changed')
    return dict(row), value, message, src, route


def _entry_approval_chain(con, draft, value, message, answer_ref):
    """Validate stored Entry evidence; no payload actor string can stand in for it."""
    from orchd import entry
    from .delivery import preview
    if type(answer_ref) is not int or answer_ref < 1:
        raise ValueError('Entry answer reference required')
    answer = con.execute('SELECT * FROM entry_messages WHERE id=?', (answer_ref,)).fetchone()
    if answer is None or answer['entry_id'] != entry.DEFAULT_ENTRY or answer['direction'] != 'in' or answer['kind'] != 'reply':
        raise ValueError('trusted Desktop Entry answer required')
    question = con.execute('SELECT * FROM entry_messages WHERE id=?', (answer['reply_to'],)).fetchone()
    binding = con.execute('SELECT * FROM entries WHERE id=?', (answer['entry_id'],)).fetchone()
    if (question is None or binding is None or question['kind'] != 'question' or question['direction'] != 'out'
            or question['entry_id'] != answer['entry_id'] or question['orch_id'] != answer['orch_id']
            or binding['orch_id'] != answer['orch_id'] or answer['orch_id'] != message['owner_orch']
            or question['question_state'] != 'answered' or question['delivery'] != 'delivered'):
        raise ValueError('Entry binding or question no longer matches')
    expected = preview(draft)
    answer_body = answer['body']
    if answer_body.endswith('\r\n'):
        answer_body = answer_body[:-2]
    elif answer_body.endswith('\n'):
        answer_body = answer_body[:-1]
    if question['body'] != expected['preview'] or answer_body != expected['answer']:
        raise ValueError('Entry answer does not approve this exact preview')
    for row in (question, answer):
        size, sha = entry.digest(row['body'])
        if (size, sha) != (row['body_bytes'], row['body_sha256']):
            raise ValueError('Entry body integrity mismatch')
    history = con.execute("SELECT * FROM entry_question_history WHERE question_id=? ORDER BY id DESC LIMIT 1", (question['id'],)).fetchone()
    if (history is None or history['to_state'] != 'answered' or history['version'] != question['version']
            or not history['actor'].startswith('operator:')):
        raise ValueError('Entry question approval history missing')
    submission = con.execute('SELECT * FROM entry_answer_submissions WHERE entry_id=? AND question_id=? AND version=? AND reply_id=?',
                             (answer['entry_id'], question['id'], question['version'], answer_ref)).fetchone()
    dashboard = submission is not None and submission['body_sha256'] == answer['body_sha256']
    desktop = (answer['source_thread'] and answer['source_thread'] == binding['thread_id'] and answer['source_item_id']
               and history['actor'] == 'operator:' + answer['source_thread'])
    if not dashboard and not desktop:
        raise ValueError('trusted Entry submission or Desktop source evidence required')


def _outbound_authority(con, draft, value, message, src, route, now):
    latest = con.execute('SELECT MAX(version) FROM intake_drafts WHERE draft_id=?', (draft['draft_id'],)).fetchone()[0]
    if latest != draft['version'] or now.timestamp() >= draft['expires_at']:
        raise ValueError('stale or expired approval')
    if (not src['enabled'] or route is None or not route['active'] or route['mode'] != 'respond'
            or route['source_id'] != src['id'] or route['responder_source_id'] != src['id']
            or src['authority_epoch'] != value['authority_epoch'] or route['authority_epoch'] != value['authority_epoch']
            or route['owner_orch'] != message['owner_orch']):
        raise ValueError('source or route authority changed')
    matched = _matching_route(con, src['id'], message['external_ref'], message['sender_ref'])
    if matched is None or (matched['id'], matched['version']) != (route['id'], route['version']):
        raise ValueError('route scope changed')


def approve_outbound(con, draft_id, version, payload_hash, answer_ref, now):
    import uuid
    from .models import canonical
    with store.immediate(con):
        draft, value, message, src, route = outbound_draft(con, draft_id, version)
        if draft['payload_hash'] != payload_hash:
            raise ValueError('approval payload hash mismatch')
        _outbound_authority(con, draft, value, message, src, route, now)
        grant = matching_grant(con, draft, value, message, now) if answer_ref is None else None
        if grant is None:
            _entry_approval_chain(con, draft, value, message, answer_ref)
        old = con.execute('SELECT * FROM intake_approvals WHERE draft_id=? AND version=?', (draft_id, version)).fetchone()
        if old and old['actor'] == 'operator:grant' and answer_ref is not None:
            delivery = con.execute("SELECT * FROM channel_deliveries WHERE kind='outbound' AND draft_id=? AND draft_version=?", (draft_id,version)).fetchone()
            if draft['state'] != 'approved' or delivery is None or delivery['state'] not in ('queued','cancelled'):
                raise ValueError('approval no longer active')
            approval_id = 'ap_' + uuid.uuid4().hex
            con.execute("UPDATE intake_approvals SET approval_id=?,actor='operator:entry',entry_answer_ref=?,created_at=? WHERE approval_id=?", (approval_id,answer_ref,now.timestamp(),old['approval_id']))
            con.execute("UPDATE channel_deliveries SET state='queued',updated_at=? WHERE id=?", (now.isoformat(),delivery['id']))
            return {'status':'approved','approval_id':approval_id,'delivery_id':delivery['id']}
        if old:
            if draft['state'] in ('cancelled','superseded','failed'):
                raise ValueError('approval no longer active')
            if old['payload_hash'] != payload_hash or old['entry_answer_ref'] != answer_ref:
                raise ValueError('approval conflict')
            delivery = con.execute("SELECT id FROM channel_deliveries WHERE kind='outbound' AND draft_id=? AND draft_version=?", (draft_id, version)).fetchone()
            if delivery is None:
                raise ValueError('approval missing delivery')
            return {'status': 'approved', 'approval_id': old['approval_id'], 'delivery_id': delivery[0]}
        if draft['state'] != 'pending_approval':
            raise ValueError('draft is not pending approval')
        if value['reply_kind'] == 'no_reply':
            raise ValueError('no_reply is never outbound')
        approval_id, delivery_id = 'ap_'+uuid.uuid4().hex, 'cd_'+uuid.uuid4().hex
        key = 'outbound:'+draft_id+':'+str(version)
        con.execute('INSERT INTO intake_approvals(approval_id,draft_id,version,body_sha256,sender,target,source_ref,expires_at,idempotency_key,actor,created_at,payload_hash,entry_answer_ref) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (approval_id,draft_id,version,draft['body_sha256'],draft['sender'],draft['target'],draft['source_ref'],draft['expires_at'],key,'operator:grant' if grant else 'operator:entry',now.timestamp(),payload_hash,answer_ref))
        con.execute("UPDATE intake_drafts SET state='approved',updated_at=? WHERE draft_id=? AND version=?", (now.timestamp(),draft_id,version))
        con.execute("INSERT INTO channel_deliveries(id,kind,source_id,message_id,draft_id,draft_version,reply_kind,route_id,route_version,authority_epoch,target_snapshot,payload_hash,state,idempotency_key,created_at,updated_at) VALUES(?,'outbound',?,?,?,?,?,?,?,?,?,?,'queued',?,?,?)",
                    (delivery_id,src['id'],message['id'],draft_id,version,value['reply_kind'],route['id'],route['version'],value['authority_epoch'],canonical({'account':value['account'],'sender':value['sender'],'target':value['target'],'thread':value['thread']}),payload_hash,key,now.isoformat(),now.isoformat()))
        if grant:
            con.execute('INSERT INTO channel_grant_approvals VALUES(?,?,?,?,?,?,?,?,?)', (approval_id,grant['grant_id'],draft_id,version,payload_hash,draft['sender'],draft['target'],now.timestamp(),delivery_id))
        return {'status':'approved','approval_id':approval_id,'delivery_id':delivery_id}


def claim_outbound(con, source_id, adapter_id, now, limit, check_enabled):
    from .models import canonical
    results = []
    with store.immediate(con):
        require_admission(con, source_id, 'adapter', sending=True)
        check_enabled()
        src = source(con, source_id)
        if not src['enabled'] or src['adapter_id'] != adapter_id:
            raise PermissionError('adapter not authorized for source')
        rows = con.execute("SELECT * FROM channel_deliveries WHERE kind='outbound' AND source_id=? AND state='queued' ORDER BY created_at,id LIMIT ?", (source_id,limit)).fetchall()
        for delivery in rows:
            try:
                draft,value,message,src,route = outbound_draft(con, delivery['draft_id'], delivery['draft_version'])
                if draft['state'] != 'approved':
                    raise ValueError('draft approval revoked')
                _outbound_authority(con,draft,value,message,src,route,now)
                approval = con.execute('SELECT * FROM intake_approvals WHERE draft_id=? AND version=?', (draft['draft_id'],draft['version'])).fetchone()
                if approval is None or approval['payload_hash'] != draft['payload_hash']:
                    raise ValueError('exact approval missing')
                if any(approval[k] != draft[k] for k in ('body_sha256','sender','target','source_ref','expires_at')):
                    raise ValueError('approval fields changed')
                evidence = con.execute('SELECT grant_id FROM channel_grant_approvals WHERE approval_id=?', (approval['approval_id'],)).fetchone()
                if evidence:
                    if matching_grant(con,draft,value,message,now,grant_id=evidence[0]) is None:
                        raise ValueError('standing grant no longer valid')
                else:
                    _entry_approval_chain(con,draft,value,message,approval['entry_answer_ref'])
                target = canonical({'account':value['account'],'sender':value['sender'],'target':value['target'],'thread':value['thread']})
                if (delivery['source_id'] != value['source_id'] or delivery['message_id'] != message['id']
                        or delivery['payload_hash'] != approval['payload_hash'] or delivery['target_snapshot'] != target
                        or delivery['route_id'] != value['route_id'] or delivery['route_version'] != value['route_version']
                        or delivery['authority_epoch'] != value['authority_epoch'] or delivery['reply_kind'] != value['reply_kind']):
                    raise ValueError('delivery snapshot changed')
            except (ValueError, PermissionError):
                con.execute("UPDATE channel_deliveries SET state='cancelled',updated_at=? WHERE id=? AND state='queued'", (now.isoformat(),delivery['id']))
                continue
            won = con.execute("UPDATE channel_deliveries SET state='claimed',updated_at=? WHERE id=? AND state='queued'", (now.isoformat(),delivery['id'])).rowcount
            if not won:
                continue
            con.execute("UPDATE intake_drafts SET state='sending',updated_at=? WHERE draft_id=? AND version=? AND state='approved'", (now.timestamp(),draft['draft_id'],draft['version']))
            con.execute("INSERT INTO channel_delivery_attempts(delivery_id,attempt_no,state,started_at) VALUES(?,1,'started',?)", (delivery['id'],now.isoformat()))
            results.append(dict(delivery, state='claimed', attempt_no=1, payload=value))
    return results


def outbound_delivery(con, delivery_id):
    row = con.execute("SELECT * FROM channel_deliveries WHERE id=? AND kind='outbound'", (delivery_id,)).fetchone()
    if row is None:
        raise ValueError('outbound delivery not found')
    return dict(row)


def delivery_attempts(con, delivery_id):
    return [dict(row) for row in con.execute('SELECT * FROM channel_delivery_attempts WHERE delivery_id=? ORDER BY attempt_no', (delivery_id,))]


def _validate_outcome(outcome, receipt):
    if outcome not in ('provider_accepted', 'failed', 'uncertain'):
        raise ValueError('invalid outbound outcome')
    if outcome == 'provider_accepted' and (not isinstance(receipt,str) or not receipt or len(receipt)>2000):
        raise ValueError('provider receipt required')
    if outcome != 'provider_accepted' and receipt is not None:
        raise ValueError('receipt belongs only to provider_accepted')


def _finish_outbound(con, delivery, outcome, receipt, now):
    state = {'provider_accepted':'sent', 'failed':'failed', 'uncertain':'uncertain'}[outcome]
    con.execute('UPDATE channel_deliveries SET state=?,updated_at=? WHERE id=?', (outcome,now.isoformat(),delivery['id']))
    con.execute('UPDATE intake_drafts SET state=?,connector_receipt_id=?,updated_at=? WHERE draft_id=? AND version=?',
                (state,receipt,now.timestamp(),delivery['draft_id'],delivery['draft_version']))
    return {'id':delivery['id'], 'state':outcome}


def record_outbound_outcome(con, delivery_id, attempt_no, outcome, receipt, adapter_id, now):
    _validate_outcome(outcome, receipt)
    if type(attempt_no) is not int or attempt_no < 1:
        raise ValueError('invalid attempt')
    with store.immediate(con):
        delivery = outbound_delivery(con,delivery_id)
        src = source(con,delivery['source_id'])
        if src['adapter_id'] != adapter_id:
            raise PermissionError('adapter identity mismatch')
        attempt = con.execute('SELECT * FROM channel_delivery_attempts WHERE delivery_id=? AND attempt_no=?', (delivery_id,attempt_no)).fetchone()
        if attempt is None:
            raise ValueError('unknown attempt')
        if delivery['state'] == outcome and attempt['state'] == outcome and attempt['provider_receipt'] == receipt:
            return {'id':delivery_id,'state':outcome}
        if delivery['state'] != 'claimed' or attempt['state'] != 'started':
            raise ValueError('attempt already ended; automatic resend refused')
        con.execute('UPDATE channel_delivery_attempts SET state=?,finished_at=?,provider_receipt=? WHERE delivery_id=? AND attempt_no=? AND state=\'started\'',
                    (outcome,now.isoformat(),receipt,delivery_id,attempt_no))
        return _finish_outbound(con,delivery,outcome,receipt,now)


def recover_outbound(con, now, *, source_id=None, in_transaction=False):
    from contextlib import nullcontext
    with nullcontext() if in_transaction else store.immediate(con):
        rows = con.execute("SELECT * FROM channel_deliveries WHERE kind='outbound' AND state='claimed' AND (? IS NULL OR source_id=?)", (source_id, source_id)).fetchall()
        for row in rows:
            con.execute("UPDATE channel_delivery_attempts SET state='uncertain',finished_at=?,error_code='abandoned_claim' WHERE delivery_id=? AND state='started'", (now.isoformat(),row['id']))
            _finish_outbound(con,row,'uncertain',None,now)
    return {'uncertain':len(rows)}


def reconcile_outbound(con, delivery_id, outcome, receipt, evidence, now):
    _validate_outcome(outcome,receipt)
    if not isinstance(evidence,str) or not evidence.strip() or len(evidence)>2000:
        raise ValueError('investigation evidence required')
    with store.immediate(con):
        delivery = outbound_delivery(con,delivery_id)
        if delivery['state'] != 'uncertain':
            raise ValueError('only uncertain may be reconciled')
        number = con.execute('SELECT COALESCE(MAX(attempt_no),0)+1 FROM channel_delivery_attempts WHERE delivery_id=?', (delivery_id,)).fetchone()[0]
        # Append an investigation record; never erase the unknown send attempt.
        con.execute('INSERT INTO channel_delivery_attempts(delivery_id,attempt_no,state,started_at,finished_at,provider_receipt,error_code) VALUES(?,?,?,?,?,?,?)',
                    (delivery_id,number,outcome,now.isoformat(),now.isoformat(),receipt,'reconcile:'+evidence))
        return _finish_outbound(con,delivery,outcome,receipt,now)


def cancel_outbound(con, draft_id, now):
    with store.immediate(con):
        row = con.execute('SELECT * FROM intake_drafts WHERE draft_id=? ORDER BY version DESC LIMIT 1', (draft_id,)).fetchone()
        if row is None or row['state'] not in ('pending_approval','approved','cancelled'):
            raise ValueError('draft cannot be cancelled after claim')
        con.execute("UPDATE intake_drafts SET state='cancelled',updated_at=? WHERE draft_id=? AND version=?", (now.timestamp(),draft_id,row['version']))
        con.execute("UPDATE channel_deliveries SET state='cancelled',updated_at=? WHERE draft_id=? AND state='queued' AND kind='outbound'", (now.isoformat(),draft_id))
    return {'status':'cancelled','draft_id':draft_id}


def _matching_route(con, source_id, conversation_ref, sender):
    import json
    candidates = con.execute("SELECT * FROM channel_routes WHERE source_id=? AND active=1 AND conversation_ref IN (?, '*')", (source_id, conversation_ref)).fetchall()
    matched = [r for r in candidates if not json.loads(r['sender_scope_json']) or sender in json.loads(r['sender_scope_json'])]
    if len(matched) > 1:
        raise ValueError('ambiguous route')
    return matched[0] if matched else None


def receive(con, source_id, event, adapter, clock, *, in_transaction=False, automation=None, accept_tombstone=False):
    from contextlib import nullcontext
    import time
    import uuid
    from datetime import datetime, timezone
    from orchd import intake, intake_bridge
    from .models import canonical, digest
    now = clock().isoformat()
    event_hash = digest({key: value for key, value in event.items() if key != 'suggestion'})
    content_hash = digest({key: value for key, value in event.items() if key not in ('transport_event_key', 'suggestion')})
    suggestion = intake_bridge.normalize_suggestion(event.get('suggestion'))
    origin = event['origin']
    origin_key = digest(origin)
    with nullcontext() if in_transaction else store.immediate(con):
        mode = require_admission(con, source_id, 'adapter' if not in_transaction else 'legacy')
        source = con.execute('SELECT * FROM channel_sources WHERE id=?', (source_id,)).fetchone()
        if source is None or not source['enabled'] or source['adapter_id'] != adapter:
            raise PermissionError('adapter not authorized for source')
        if (source['provider'],source['tenant_ref']) != (origin['provider'],origin['tenant_ref']):
            raise PermissionError('source origin mismatch')
        if accept_tombstone and con.execute("SELECT 1 FROM channel_messages m JOIN intake_content c ON c.receipt_id=m.intake_receipt_id WHERE m.origin_key=? AND m.deleted_at IS NOT NULL AND c.unavailable_reason='deleted' LIMIT 1", (origin_key,)).fetchone():
            previous = con.execute('SELECT origin_key,content_hash FROM channel_observations WHERE source_id=? AND transport_event_key=?',(source_id,event['transport_event_key'])).fetchone()
            if previous and (previous['origin_key'] != origin_key or (previous['content_hash'] != event_hash and event['content'].get('unavailable_reason') != 'deleted')):
                raise ValueError('conflict')
            tomb = con.execute('SELECT id,intake_receipt_id FROM channel_messages WHERE origin_key=? AND deleted_at IS NOT NULL LIMIT 1',(origin_key,)).fetchone()
            con.execute('INSERT OR IGNORE INTO channel_observations VALUES(?,?,?,?,?,?)',(source_id,event['transport_event_key'],origin_key,tomb['id'],event_hash,now))
            return {'status':'duplicate','message_id':tomb['id'],'receipt_id':tomb['intake_receipt_id']}
        old = con.execute('SELECT * FROM channel_observations WHERE source_id=? AND transport_event_key=?', (source_id,event['transport_event_key'])).fetchone()
        if old:
            if old['content_hash'] != event_hash:
                raise ValueError('conflict')
            row = con.execute('SELECT intake_receipt_id,content_hash FROM channel_messages WHERE id=?',(old['message_id'],)).fetchone()
            if row['content_hash'] != content_hash:
                return {'status':'conflict','message_id':old['message_id'],'receipt_id':row['intake_receipt_id'],'review_required':True}
            state = con.execute('SELECT state FROM intake_events WHERE receipt_id=?', (row[0],)).fetchone()
            if suggestion and state and state[0] == 'received' and mode != 'shadow':
                from orchd import intake_notify
                _, appended = intake_bridge._add_suggestion(con, row[0], suggestion, adapter)
                if appended:
                    intake_notify.upgrade(con, row[0], intake_bridge._suggestion_urgency(suggestion))
            owner_row = con.execute('SELECT owner_orch FROM intake_events WHERE receipt_id=?', (row[0],)).fetchone()
            if mode != 'adapter' or owner_row[0] is not None or not con.execute('SELECT 1 FROM channel_cutovers WHERE source_id=?', (source_id,)).fetchone():
                return {'status':'duplicate','message_id':old['message_id'],'receipt_id':row[0]}
        if con.execute("SELECT 1 FROM channel_messages m JOIN intake_content c ON c.receipt_id=m.intake_receipt_id WHERE m.origin_key=? AND m.deleted_at IS NOT NULL AND c.unavailable_reason='deleted' LIMIT 1", (origin_key,)).fetchone():
            raise ValueError('origin tombstoned')
        route = _matching_route(con, source_id, origin['conversation_ref'], event['sender_ref'])
        owner = route['owner_orch'] if route and route['mode'] != 'observer' and mode != 'shadow' else None
        existing = con.execute('SELECT * FROM channel_messages WHERE origin_key=? AND revision_key=?', (origin_key,event['revision_key'])).fetchone()
        if existing:
            import json
            if json.loads(existing['provenance_json']).get('loop_status', 'human') not in ('human','allowed'):
                owner = None
            if existing['content_hash'] != content_hash:
                con.execute('INSERT INTO channel_observations VALUES(?,?,?,?,?,?)',(source_id,event['transport_event_key'],origin_key,existing['id'],event_hash,now))
                return {'status':'conflict','message_id':existing['id'],'receipt_id':existing['intake_receipt_id'],'review_required':True}
            previous_owner = con.execute('SELECT owner_orch FROM intake_events WHERE receipt_id=?', (existing['intake_receipt_id'],)).fetchone()[0]
            if owner and previous_owner and owner != previous_owner:
                raise ValueError('origin owner conflict')
            if owner and not previous_owner:
                from orchd import intake_notify
                conversation_id = 'cc_' + uuid.uuid4().hex
                con.execute('INSERT OR IGNORE INTO channel_conversations(id,source_id,external_ref,kind,thread_key) VALUES(?,?,?,?,?)',
                            (conversation_id,source_id,origin['conversation_ref'],event['conversation']['kind'],event['conversation']['thread_key']))
                conversation_id = con.execute('SELECT id FROM channel_conversations WHERE source_id=? AND external_ref=? AND thread_key=?',
                                              (source_id,origin['conversation_ref'],event['conversation']['thread_key'])).fetchone()[0]
                con.execute('UPDATE channel_messages SET conversation_id=? WHERE id=?', (conversation_id, existing['id']))
                con.execute('UPDATE intake_events SET owner_orch=? WHERE receipt_id=?', (owner, existing['intake_receipt_id']))
                con.execute("INSERT INTO channel_processing_claims VALUES(?,?,?,?,?,'claimed',?)", (origin_key,event['revision_key'],'default',owner,route['authority_epoch'],now))
                intake_notify.enqueue(con, existing['intake_receipt_id'], owner)
                con.execute("INSERT INTO channel_deliveries(id,kind,source_id,message_id,route_id,route_version,authority_epoch,target_snapshot,payload_hash,state,idempotency_key,created_at,updated_at) VALUES(?,'wake',?,?,?,?,?,?,?,'queued',?,?,?)",
                            ('cd_'+uuid.uuid4().hex,source_id,existing['id'],route['id'],route['version'],route['authority_epoch'],canonical({'owner_orch':owner}),content_hash,'wake:'+existing['id'],now,now))
            con.execute('INSERT OR IGNORE INTO channel_observations VALUES(?,?,?,?,?,?)',(source_id,event['transport_event_key'],origin_key,existing['id'],event_hash,now))
            return {'status':'duplicate','message_id':existing['id'],'receipt_id':existing['intake_receipt_id']}
        loop_status, provenance = _loop_guard(con, origin, automation, now)
        if loop_status not in ('human', 'allowed') or event['content'].get('unavailable_reason') == 'deleted':
            owner = None
        conversation_id='cc_'+uuid.uuid4().hex
        con.execute('INSERT OR IGNORE INTO channel_conversations(id,source_id,external_ref,kind,thread_key) VALUES(?,?,?,?,?)',(conversation_id,source_id,origin['conversation_ref'],event['conversation']['kind'],event['conversation']['thread_key']))
        conversation_id=con.execute('SELECT id FROM channel_conversations WHERE source_id=? AND external_ref=? AND thread_key=?',(source_id,origin['conversation_ref'],event['conversation']['thread_key'])).fetchone()[0]
        con.execute('INSERT OR IGNORE INTO channel_origins VALUES(?,?,?,?,?)',(origin_key,origin['provider'],origin['tenant_ref'],origin['conversation_ref'],origin['message_id']))
        message_id='cm_'+uuid.uuid4().hex
        receipt_id='ir_'+uuid.uuid4().hex
        content=event['content']
        text=content.get('text')
        source_time=datetime.fromisoformat(event['source_time'].replace('Z','+00:00')).timestamp()
        provider='email' if source['provider']=='gmail' else source['provider']
        con.execute('INSERT INTO intake_events(receipt_id,version,provider,account,company,event_id,source_ref,thread_ref,source_time,received_at,producer,summary,content_sha256,owner_orch) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(receipt_id,2,provider,source['account_ref'],source['tenant_ref'] or source['account_ref'],event['transport_event_key'],origin['conversation_ref'],event['conversation']['thread_key'] or None,source_time,time.time(),adapter,'Channel message',event_hash,owner))
        metadata = {'mentions':content.get('mentions', []), 'format':content.get('format'), 'target':event.get('target')}
        con.execute('INSERT INTO intake_content(receipt_id,status,text,unavailable_reason,text_sha256,text_bytes,attachments,created_at,channel_metadata) VALUES(?,?,?,?,?,?,?,?,?)',(receipt_id,content['status'],text,content.get('unavailable_reason'),intake._sha(text) if text is not None else None,len(text.encode()) if text else 0,canonical(content.get('attachments',[])),time.time(),canonical(metadata)))
        intake._log(con,'event',receipt_id)
        if suggestion:
            intake_bridge._add_suggestion(con, receipt_id, suggestion, adapter)
        con.execute('INSERT INTO channel_messages(id,conversation_id,origin_key,provider_message_id,revision_key,direction,trust,sender_ref,source_time,received_at,content_hash,intake_receipt_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(message_id,conversation_id,origin_key,origin['message_id'],event['revision_key'],'in','third_party_data',event['sender_ref'],event['source_time'],now,content_hash,receipt_id))
        import json
        provenance = canonical(dict(json.loads(provenance), loop_status=loop_status))
        con.execute('UPDATE channel_messages SET provenance_json=? WHERE id=?',(provenance,message_id))
        con.execute('INSERT INTO channel_observations VALUES(?,?,?,?,?,?)',(source_id,event['transport_event_key'],origin_key,message_id,event_hash,now))
        for index, attachment in enumerate(content.get('attachments', [])):
            ref = attachment.get('provider_attachment_ref') or f'legacy-index:{index}'
            con.execute('INSERT INTO channel_attachments(id,message_id,provider_attachment_ref,original_name,sha256,byte_size,status,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                        ('ca_'+uuid.uuid4().hex, message_id, ref, attachment['name'], attachment.get('sha256'), attachment.get('bytes'), attachment['status'], now))
        if owner:
            from orchd import intake_notify
            con.execute("INSERT INTO channel_processing_claims(origin_key,revision_key,lane,owner_orch,authority_epoch,state,created_at) VALUES(?,?,?,?,?,'claimed',?)", (origin_key,event['revision_key'],'default',owner,route['authority_epoch'],now))
            intake_notify.enqueue(con,receipt_id,owner,intake_bridge._suggestion_urgency(suggestion))
            con.execute('INSERT INTO channel_deliveries(id,kind,source_id,message_id,route_id,route_version,authority_epoch,target_snapshot,payload_hash,state,idempotency_key,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',('cd_'+uuid.uuid4().hex,'wake',source_id,message_id,route['id'],route['version'],route['authority_epoch'],canonical({'owner_orch':owner}),event_hash,'queued','wake:'+message_id,now,now))
        if event['content'].get('unavailable_reason') == 'deleted':
            tombstone(con,message_id,source_id,in_transaction=True)
        return {'status':'received','message_id':message_id,'receipt_id':receipt_id,'loop_status':loop_status}


def get_message(con, message_id):
    row = con.execute('SELECT m.*, c.source_id, c.external_ref, e.owner_orch FROM channel_messages m JOIN channel_conversations c ON c.id=m.conversation_id LEFT JOIN intake_events e ON e.receipt_id=m.intake_receipt_id WHERE m.id=?', (message_id,)).fetchone()
    if row is None:
        raise ValueError('message not found')
    return dict(row)


def list_messages(con, source_id, limit, after='', owner=None):
    return [dict(r) for r in con.execute('SELECT m.* FROM channel_messages m JOIN channel_conversations c ON c.id=m.conversation_id LEFT JOIN intake_events e ON e.receipt_id=m.intake_receipt_id WHERE c.source_id=? AND m.id>? AND (? IS NULL OR e.owner_orch IS NULL OR e.owner_orch=?) ORDER BY m.id LIMIT ?', (source_id,after,owner,owner,limit))]


def content(con, receipt_id):
    row = con.execute('SELECT status,text,unavailable_reason,attachments,channel_metadata FROM intake_content WHERE receipt_id=?',(receipt_id,)).fetchone()
    return dict(row) if row else {'status':'unavailable','text':None,'unavailable_reason':'legacy_v1','attachments':'[]','channel_metadata':'{}'}


def sync_request(con, source_id, key, scope, caller):
    import uuid
    from datetime import datetime, timezone
    from .models import canonical, digest
    request_hash = digest({'scope': scope, 'requested_by': caller})
    with store.immediate(con):
        old = con.execute('SELECT * FROM channel_sync_jobs WHERE source_id=? AND request_key=?',(source_id,key)).fetchone()
        if old:
            if old['request_hash'] != request_hash:
                raise ValueError('sync request conflict')
            return dict(old)
        job='cs_'+uuid.uuid4().hex
        con.execute('INSERT INTO channel_sync_jobs(id,source_id,request_key,request_hash,requested_by,scope_json,state,created_at) VALUES(?,?,?,?,?,?,?,?)',(job,source_id,key,request_hash,caller,canonical(scope),'pending',datetime.now(timezone.utc).isoformat()))
        return dict(con.execute('SELECT * FROM channel_sync_jobs WHERE id=?',(job,)).fetchone())


def sync_status(con, job):
    row=con.execute('SELECT * FROM channel_sync_jobs WHERE id=?',(job,)).fetchone()
    if row is None:
        raise ValueError('unknown sync job')
    return dict(row)


def attachment(con, attachment_id):
    row=con.execute('SELECT a.*, c.source_id, e.owner_orch FROM channel_attachments a JOIN channel_messages m ON m.id=a.message_id JOIN channel_conversations c ON c.id=m.conversation_id LEFT JOIN intake_events e ON e.receipt_id=m.intake_receipt_id WHERE a.id=?',(attachment_id,)).fetchone()
    if row is None:
        raise ValueError('unknown attachment')
    return dict(row)


def queue_attachment(con, attachment_id):
    with store.immediate(con):
        con.execute("UPDATE channel_attachments SET status='queued' WHERE id=? AND status='referenced'",(attachment_id,))


def message_attachments(con, message_id):
    return [dict(row) for row in con.execute('SELECT id,provider_attachment_ref,status FROM channel_attachments WHERE message_id=? ORDER BY rowid', (message_id,))]


def queued_attachments(con, source_id):
    return [dict(row) for row in con.execute("SELECT a.*,m.provider_message_id FROM channel_attachments a JOIN channel_messages m ON m.id=a.message_id JOIN channel_conversations c ON c.id=m.conversation_id WHERE c.source_id=? AND a.status='queued' ORDER BY a.id", (source_id,))]


def attachment_available(con, attachment_id, source_id, digest, size, now, holder, generation):
    with store.immediate(con):
        _require_lease(con, source_id, holder, generation, now)
        count=con.execute("UPDATE channel_attachments SET status='available',sha256=?,storage_key=?,byte_size=?,updated_at=? WHERE id=? AND status='queued' AND message_id IN (SELECT m.id FROM channel_messages m JOIN channel_conversations c ON c.id=m.conversation_id WHERE c.source_id=? AND m.deleted_at IS NULL)", (digest,digest,size,now,attachment_id,source_id)).rowcount
        if count != 1:
            raise ValueError('attachment no longer queued')



def tombstone(con, message_id, source_id, *, reason='deleted', in_transaction=False):
    from datetime import datetime, timezone
    from contextlib import nullcontext
    with nullcontext() if in_transaction else store.immediate(con):
        row=con.execute('SELECT m.origin_key FROM channel_messages m JOIN channel_conversations c ON c.id=m.conversation_id WHERE m.id=? AND c.source_id=?',(message_id,source_id)).fetchone()
        if row is None:
            raise PermissionError('message source mismatch')
        now=datetime.now(timezone.utc).isoformat()
        predicate, target = ('origin_key=?', row[0]) if reason == 'deleted' else ('id=?', message_id)
        con.execute(f"UPDATE intake_content SET status='unavailable',text=NULL,text_sha256=NULL,text_bytes=0,attachments='[]',channel_metadata='{{}}',unavailable_reason=? WHERE receipt_id IN (SELECT intake_receipt_id FROM channel_messages WHERE {predicate})",(reason,target))
        con.execute(f"UPDATE interface_requests SET body='',reply=CASE WHEN reply IS NULL THEN NULL ELSE '' END,interim=CASE WHEN interim IS NULL THEN NULL ELSE '' END WHERE CAST(id AS TEXT) IN (SELECT old_id FROM channel_legacy_refs WHERE system='interface' AND new_kind='message' AND new_id IN (SELECT id FROM channel_messages WHERE {predicate}))", (target,))
        con.execute(f"UPDATE intake_drafts SET body='' WHERE receipt_id IN (SELECT intake_receipt_id FROM channel_messages WHERE {predicate})", (target,))
        con.execute(f"UPDATE channel_attachments SET status='deleted',original_name=NULL,sha256=NULL,byte_size=NULL,updated_at=? WHERE message_id IN (SELECT id FROM channel_messages WHERE {predicate})",(now,target))
        con.execute(f'UPDATE channel_messages SET deleted_at=? WHERE {predicate}',(now,target))


def pending_deleted_attachments(con):
    return [dict(row) for row in con.execute("SELECT a.id, a.storage_key, c.source_id, s.provider FROM channel_attachments a JOIN channel_messages m ON m.id=a.message_id JOIN channel_conversations c ON c.id=m.conversation_id JOIN channel_sources s ON s.id=c.source_id WHERE a.status='deleted' AND a.storage_key IS NOT NULL")]


def clear_deleted_storage(con, attachment_id, key):
    with store.immediate(con):
        con.execute("UPDATE channel_attachments SET storage_key=NULL WHERE id=? AND status='deleted' AND storage_key=?", (attachment_id, key))


def source(con, source_id):
    row=con.execute('SELECT * FROM channel_sources WHERE id=?',(source_id,)).fetchone()
    if row is None:
        raise ValueError('unknown source')
    return dict(row)


def acquire_source_lease(con, source_id, holder, now, until):
    with store.immediate(con):
        require_admission(con, source_id, 'adapter')
        row = con.execute('SELECT * FROM channel_source_leases WHERE source_id=?', (source_id,)).fetchone()
        if row and row['lease_until'] > now:
            return None
        generation = row['generation'] + 1 if row else 1
        con.execute('INSERT INTO channel_source_leases VALUES(?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET holder_id=excluded.holder_id,generation=excluded.generation,lease_until=excluded.lease_until',
                    (source_id, holder, generation, until))
        return generation


def release_source_lease(con, source_id, holder, generation, now):
    with store.immediate(con):
        changed = con.execute('UPDATE channel_source_leases SET lease_until=? WHERE source_id=? AND holder_id=? AND generation=? AND lease_until>?',
                              (now, source_id, holder, generation, now)).rowcount
        if changed != 1:
            raise PermissionError('source lease lost')


def renew_source_lease(con, source_id, holder, generation, now, until):
    with store.immediate(con):
        require_admission(con, source_id, 'adapter')
        count = con.execute('UPDATE channel_source_leases SET lease_until=? WHERE source_id=? AND holder_id=? AND generation=? AND lease_until>?',
                            (until, source_id, holder, generation, now)).rowcount
        if count != 1:
            raise PermissionError('source lease lost')


def checkpoint(con, source_id, scope):
    row = con.execute('SELECT * FROM channel_checkpoints WHERE source_id=? AND scope=?', (source_id, scope)).fetchone()
    return dict(row) if row else None


def commit_checkpoint(con, source_id, scope, cursor, receipts, holder, generation, now):
    from .models import digest
    with store.immediate(con):
        require_admission(con, source_id, 'adapter')
        lease = con.execute('SELECT * FROM channel_source_leases WHERE source_id=?', (source_id,)).fetchone()
        if not lease or lease['holder_id'] != holder or lease['generation'] != generation or lease['lease_until'] <= now:
            raise PermissionError('source lease lost')
        for receipt in receipts:
            if not con.execute('SELECT 1 FROM channel_observations o JOIN channel_messages m ON m.id=o.message_id WHERE o.source_id=? AND m.intake_receipt_id=?', (source_id, receipt)).fetchone():
                raise ValueError('receipt not durable for source')
        old = checkpoint(con, source_id, scope)
        backwards = old and ((int(old['committed_cursor']) > int(cursor)) if scope == 'history' or scope.startswith(('history:', 'thread:')) else (old['committed_cursor'] > cursor))
        if backwards:
            raise ValueError('checkpoint cannot move backwards')
        con.execute('INSERT INTO channel_checkpoints VALUES(?,?,?,?,?,?) ON CONFLICT(source_id,scope) DO UPDATE SET committed_cursor=excluded.committed_cursor,window_end=excluded.window_end,receipt_set_hash=excluded.receipt_set_hash,updated_at=excluded.updated_at',
                    (source_id, scope, cursor, cursor, digest(sorted(receipts)), now))


def _require_lease(con, source_id, holder, generation, now):
    require_admission(con, source_id, 'adapter')
    row = con.execute('SELECT * FROM channel_source_leases WHERE source_id=?', (source_id,)).fetchone()
    if not row or row['holder_id'] != holder or row['generation'] != generation or row['lease_until'] <= now:
        raise PermissionError('source lease lost')


def record_adapter_health(con, source_id, holder, generation, now, state):
    with store.immediate(con):
        _require_lease(con, source_id, holder, generation, now)
        con.execute("INSERT INTO channel_checkpoints VALUES(?,'adapter_health',?,NULL,?,?) ON CONFLICT(source_id,scope) DO UPDATE SET committed_cursor=excluded.committed_cursor,receipt_set_hash=excluded.receipt_set_hash,updated_at=excluded.updated_at", (source_id, now, state, now))


def claim_sync_job(con, source_id, holder, generation, now):
    with store.immediate(con):
        _require_lease(con, source_id, holder, generation, now)
        row = con.execute("SELECT id FROM channel_sync_jobs WHERE source_id=? AND state IN ('pending','running') ORDER BY created_at,id LIMIT 1", (source_id,)).fetchone()
        if not row:
            return None
        con.execute("UPDATE channel_sync_jobs SET state='running',error_code=NULL,completed_at=NULL WHERE id=?", (row['id'],))
        return row['id']


def finish_sync_job(con, source_id, job_id, holder, generation, now, error):
    with store.immediate(con):
        if error:
            lease = con.execute('SELECT holder_id,generation FROM channel_source_leases WHERE source_id=?', (source_id,)).fetchone()
            if not lease or lease['holder_id'] != holder or lease['generation'] != generation:
                raise PermissionError('source lease lost')
        else:
            _require_lease(con, source_id, holder, generation, now)
        count = con.execute("UPDATE channel_sync_jobs SET state=?,completed_at=?,error_code=? WHERE id=? AND source_id=? AND state='running'",
                            ('failed' if error else 'succeeded', now, error, job_id, source_id)).rowcount
        if count != 1:
            raise ValueError('sync job not running')


def observed_by(con, message_id, source_id):
    return con.execute('SELECT 1 FROM channel_observations WHERE message_id=? AND source_id=?', (message_id, source_id)).fetchone() is not None





def source_ids(con):
    return frozenset(r[0] for r in con.execute('SELECT id FROM channel_sources'))


def conversation_source(con, conversation_id):
    row=con.execute('SELECT source_id FROM channel_conversations WHERE id=?',(conversation_id,)).fetchone()
    if row is None:
        raise ValueError('unknown conversation')
    return row[0]


def conversation_messages(con, conversation_id, cursor, limit, owner=None):
    return [r[0] for r in con.execute('SELECT m.id FROM channel_messages m LEFT JOIN intake_events e ON e.receipt_id=m.intake_receipt_id WHERE m.conversation_id=? AND m.id>? AND (? IS NULL OR e.owner_orch IS NULL OR e.owner_orch=?) ORDER BY m.id LIMIT ?', (conversation_id, cursor, owner, owner, limit))]


def draft(con, message, payload, clock, caller):
    import json
    from orchd import intake
    from .delivery import normalize_payload
    source_row=source(con,message['source_id'])
    conversation = con.execute('SELECT thread_key FROM channel_conversations WHERE id=?', (message['conversation_id'],)).fetchone()
    route = _matching_route(con, source_row['id'], message['external_ref'], message['sender_ref'])
    exact = normalize_payload(payload, source_row, message, route, conversation['thread_key'])
    if exact['reply_kind'] == 'no_reply':
        return {'status': 'no_reply', 'message_id': message['id']}
    result = intake.draft(con,{'draft_id':payload.get('draft_id'), 'receipt_id':message['intake_receipt_id'],
                             'provider':'email' if source_row['provider']=='gmail' else source_row['provider'],
                             'sender':payload['sender'],'target':json.dumps(exact['target'], sort_keys=True) if isinstance(exact['target'],dict) else exact['target'],
                             'source_ref':message['external_ref'],'body':payload['body'],
                             'summary':'Channel reply draft','expires_at':payload['expires_at']},actor='orch', _channel_payload=exact, _now=clock().timestamp())
    result['payload_hash'] = intake._draft_row(con, result['draft_id'], result['version'])['payload_hash']
    con.execute('INSERT INTO channel_draft_authors VALUES(?,?,?,?,?)', (result['draft_id'],result['version'],caller.role,caller.identity,result['payload_hash']))
    return result


def routes(con):
    return [dict(row) for row in con.execute('SELECT * FROM channel_routes WHERE active=1 ORDER BY id')]


def apply_routes(con, entries):
    from datetime import datetime, timezone
    from .models import canonical, digest
    now=datetime.now(timezone.utc).isoformat()
    with store.immediate(con):
        con.execute('UPDATE channel_routes SET active=0 WHERE active=1')
        for route in entries:
            previous=con.execute('SELECT COALESCE(MAX(version),0) FROM channel_routes WHERE id=?',(route['id'],)).fetchone()[0]
            scope=digest({'source':route['source_id'],'conversation':route['conversation_ref'],'senders':sorted(route.get('senders',[]))})
            con.execute('INSERT INTO channel_routes(id,version,scope_key,source_id,conversation_ref,sender_scope_json,project_ref,repo_ref,owner_orch,responder_source_id,mode,authority_epoch,active,effective_at,manifest_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)', (route['id'],previous+1,scope,route['source_id'],route['conversation_ref'],canonical(route.get('senders',[])),route.get('project_ref'),route.get('repo_ref'),route['owner_orch'],route.get('responder_source_id'),route.get('mode','receive_only'),route.get('authority_epoch',1),now,digest(entries)))


def link_legacy_intake(con, source_id, receipt_id, *, in_transaction=False):
    from contextlib import nullcontext
    import json
    import uuid
    from datetime import datetime, timezone
    from .models import digest
    with nullcontext() if in_transaction else store.immediate(con):
        require_admission(con, source_id, 'legacy')
        prior=con.execute("SELECT new_id FROM channel_legacy_refs WHERE system='intake' AND old_id=? AND new_kind='message'",(receipt_id,)).fetchone()
        if prior:
            return {'status':'linked','message_id':prior[0],'receipt_id':receipt_id}
        event=con.execute('SELECT * FROM intake_events WHERE receipt_id=?',(receipt_id,)).fetchone()
        src=con.execute('SELECT * FROM channel_sources WHERE id=?',(source_id,)).fetchone()
        if event is None or src is None:
            raise ValueError('unknown source or legacy receipt')
        provider='email' if src['provider']=='gmail' else src['provider']
        if (event['provider'],event['account']) != (provider,src['account_ref']):
            raise PermissionError('legacy source mismatch')
        now=datetime.now(timezone.utc).isoformat()
        ref=event['source_ref']
        origin={'provider':src['provider'],'tenant_ref':src['tenant_ref'],'conversation_ref':ref,'message_id':event['event_id']}
        origin_key=digest(origin)
        conversation_id='cc_'+uuid.uuid4().hex
        kind='email' if src['provider']=='gmail' else 'channel'
        thread=event['thread_ref'] or ''
        con.execute('INSERT OR IGNORE INTO channel_conversations(id,source_id,external_ref,kind,thread_key) VALUES(?,?,?,?,?)',(conversation_id,source_id,ref,kind,thread))
        conversation_id=con.execute('SELECT id FROM channel_conversations WHERE source_id=? AND external_ref=? AND thread_key=?',(source_id,ref,thread)).fetchone()[0]
        con.execute('INSERT OR IGNORE INTO channel_origins VALUES(?,?,?,?,?)',(origin_key,src['provider'],src['tenant_ref'],ref,event['event_id']))
        con.execute("INSERT OR IGNORE INTO intake_content(receipt_id,status,text,unavailable_reason,attachments,created_at) VALUES(?,'unavailable',NULL,'legacy_v1','[]',?)",(receipt_id,event['received_at']))
        content = con.execute('SELECT * FROM intake_content WHERE receipt_id=?', (receipt_id,)).fetchone()
        normalized_content = {'status': content['status'], 'text': content['text'], 'attachments': json.loads(content['attachments'])}
        if content['unavailable_reason']:
            normalized_content['unavailable_reason'] = content['unavailable_reason']
        source_time = datetime.fromtimestamp(event['source_time'],timezone.utc).isoformat()
        normalized = {'schema_version':1, 'transport_event_key':event['event_id'], 'origin':origin,
                      'revision_key':'initial', 'conversation':{'kind':kind, 'thread_key':thread},
                      'sender_ref':event['producer'], 'source_time':source_time, 'content':normalized_content}
        content_hash = digest({key:value for key,value in normalized.items() if key != 'transport_event_key'})
        mid='cm_'+uuid.uuid4().hex
        con.execute('INSERT INTO channel_messages(id,conversation_id,origin_key,provider_message_id,revision_key,direction,trust,sender_ref,source_time,received_at,content_hash,intake_receipt_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(mid,conversation_id,origin_key,event['event_id'],'initial','in','third_party_data',event['producer'],source_time,datetime.fromtimestamp(event['received_at'],timezone.utc).isoformat(),content_hash,receipt_id))
        con.execute('INSERT INTO channel_observations VALUES(?,?,?,?,?,?)',(source_id,event['event_id'],origin_key,mid,digest(normalized),now))
        con.execute("INSERT INTO channel_legacy_refs VALUES('intake',?,'message',?)",(receipt_id,mid))
        matches = [r for r in con.execute("SELECT * FROM channel_routes WHERE source_id=? AND active=1 AND conversation_ref IN (?, '*')", (source_id, ref)) if not json.loads(r['sender_scope_json'])]
        if len(matches) > 1:
            raise ValueError('ambiguous legacy route')
        route = matches[0] if matches and matches[0]['mode'] != 'observer' else None
        if route and event['owner_orch'] not in (None, route['owner_orch']):
            raise ValueError('legacy owner conflicts with route')
        if route and event['state'] == 'received':
            from orchd import intake_notify
            owner=route['owner_orch']
            con.execute('UPDATE intake_events SET owner_orch=? WHERE receipt_id=?', (owner,receipt_id))
            con.execute("INSERT INTO channel_processing_claims VALUES(?,?,?, ?,?,'claimed',?)",(origin_key,'initial','default',owner,route['authority_epoch'],now))
            intake_notify.enqueue(con,receipt_id,owner)
            con.execute("INSERT INTO channel_deliveries(id,kind,source_id,message_id,route_id,route_version,authority_epoch,target_snapshot,payload_hash,state,idempotency_key,created_at,updated_at) VALUES(?, 'wake',?,?,?,?,?,?,?,'queued',?,?,?)", ('cd_'+uuid.uuid4().hex,source_id,mid,route['id'],route['version'],route['authority_epoch'],json.dumps({'owner_orch':owner},sort_keys=True),event['content_sha256'],'wake:'+mid,now,now))
        return {'status':'linked','message_id':mid,'receipt_id':receipt_id}


def retention_candidates(con):
    return [dict(row) for row in con.execute('SELECT m.id, m.received_at, c.source_id FROM channel_messages m JOIN channel_conversations c ON c.id=m.conversation_id WHERE m.deleted_at IS NULL')]


def link_legacy_interface(con, source_id, request_id, interface_row):
    """Map an authenticated legacy request before its surrounding transaction commits."""
    import json
    from datetime import datetime, timezone
    from . import models
    old_id=str(request_id)
    prior=con.execute("SELECT new_id FROM channel_legacy_refs WHERE system='interface' AND old_id=? AND new_kind='message'",(old_id,)).fetchone()
    if prior:
        return prior[0]
    request=con.execute('SELECT * FROM interface_requests WHERE id=?',(request_id,)).fetchone()
    src=source(con,source_id)
    sender='legacy-interface:'+interface_row['name']
    routes=con.execute("SELECT * FROM channel_routes WHERE source_id=? AND active=1 AND conversation_ref IN (?, '*')",(source_id,request['conversation'])).fetchall()
    matched=[r for r in routes if not json.loads(r['sender_scope_json']) or sender in json.loads(r['sender_scope_json'])]
    if len(matched) != 1 or matched[0]['owner_orch'] != interface_row['orch_id'] or matched[0]['mode'] == 'observer':
        raise ValueError('legacy interface needs an explicit route to its owner')
    event={'schema_version':1,
           'transport_event_key':models.digest({'conversation':request['conversation'],'key':request['request_key']}),
           'origin':{'provider':src['provider'],'tenant_ref':src['tenant_ref'],'conversation_ref':request['conversation'],'message_id':request['request_key']},
           'revision_key':'initial',
           'conversation':{'kind':'email' if src['provider']=='gmail' else 'channel','thread_key':''},
           'sender_ref':sender,
           'source_time':datetime.fromtimestamp(request['created_at'],timezone.utc).isoformat(),
           'content':{'status':'complete','text':request['body'],'attachments':[]}}
    result=receive(con,source_id,models.validate_event(event),src['adapter_id'],lambda: datetime.now(timezone.utc),in_transaction=True)
    con.execute("INSERT INTO channel_legacy_refs VALUES('interface',?,'message',?)",(old_id,result['message_id']))
    return result['message_id']


def worker_identity(con, worktree):
    from pathlib import Path
    resolved=Path(worktree).resolve()
    matches=[]
    for row in con.execute("SELECT id,worktree,orch_thread,status FROM tasks WHERE worktree IS NOT NULL"):
        if Path(row['worktree']).resolve() == resolved:
            matches.append(row)
    if len(matches) != 1 or matches[0]['status'] not in ('starting','running','acked','question'):
        raise PermissionError('worker identity unavailable')
    return {'id':matches[0]['id'],'owner_orch':matches[0]['orch_thread']}


def database_path(con):
    return con.execute("PRAGMA database_list").fetchone()[2]


def require_admission(con, source_id, lane, *, sending=False):
    """Check the durable fence inside the caller's write transaction."""
    if not con.execute("SELECT 1 FROM sqlite_master WHERE name='channel_cutovers'").fetchone():
        return 'adapter'
    row = con.execute('SELECT mode FROM channel_cutovers WHERE source_id=?', (source_id,)).fetchone()
    if row is None:  # Prior compatibility; rollout must explicitly fence the source.
        return 'adapter'
    mode = row[0]
    if mode == 'frozen' or (lane == 'legacy' and mode != 'legacy') or (lane == 'adapter' and mode == 'legacy') or (sending and mode != 'adapter'):
        raise PermissionError('source admission fenced')
    return mode


def require_legacy_admission(con, *, provider=None, account=None, adapter=None):
    if not con.execute("SELECT 1 FROM sqlite_master WHERE name='channel_sources'").fetchone():
        return
    provider = 'gmail' if provider == 'email' else provider
    rows = con.execute('SELECT id FROM channel_sources WHERE (provider=? AND account_ref=?) OR adapter_id=?', (provider, account, adapter))
    for row in rows:
        require_admission(con, row[0], 'legacy')


def cutover_ledger(con, source_id, *, after=0, through=None):
    """Metadata-only ledger at a fixed message high water; no private content."""
    from .models import digest
    source(con, source_id)
    if type(after) is not int or after < 0 or (through is not None and (type(through) is not int or through < after)):
        raise ValueError('invalid high-water range')
    high = con.execute('SELECT COALESCE(MAX(rowid),0) FROM channel_messages').fetchone()[0] if through is None else through
    messages = [dict(r) for r in con.execute('SELECT m.rowid AS sequence,m.id,m.origin_key,m.revision_key,m.intake_receipt_id,m.deleted_at FROM channel_messages m JOIN channel_observations o ON o.message_id=m.id WHERE o.source_id=? AND m.rowid>? AND m.rowid<=? GROUP BY m.id ORDER BY m.rowid', (source_id, after, high))]
    deliveries = [dict(r) for r in con.execute('SELECT id,kind,state,idempotency_key FROM channel_deliveries WHERE source_id=? ORDER BY id', (source_id,))]
    attempts = [dict(r) for r in con.execute('SELECT a.delivery_id,a.attempt_no,a.state FROM channel_delivery_attempts a JOIN channel_deliveries d ON d.id=a.delivery_id WHERE d.source_id=? ORDER BY a.delivery_id,a.attempt_no', (source_id,))]
    work = [dict(r) for r in con.execute('SELECT DISTINCT p.origin_key,p.revision_key,p.lane,p.state FROM channel_processing_claims p JOIN channel_messages m ON m.origin_key=p.origin_key AND m.revision_key=p.revision_key JOIN channel_observations o ON o.message_id=m.id WHERE o.source_id=? ORDER BY p.origin_key,p.revision_key,p.lane', (source_id,))]
    result = dict(schema_version=1,source_id=source_id,after=after,high_water=high,messages=messages,deliveries=deliveries,attempts=attempts,work=work)
    return {**result, 'digest': digest(result)}


def cutover(con, source_id, mode, now, backfill=None):
    if mode not in ('adapter', 'legacy', 'shadow', 'frozen'):
        raise ValueError('invalid cutover mode')
    with store.immediate(con):
        source(con, source_id)
        old = con.execute('SELECT mode FROM channel_cutovers WHERE source_id=?', (source_id,)).fetchone()
        previous = old[0] if old else 'adapter'
        if mode != previous:
            lease = con.execute('SELECT lease_until FROM channel_source_leases WHERE source_id=?', (source_id,)).fetchone()
            if lease and lease[0] > now.isoformat():
                raise ValueError('stop and release the adapter before cutover')
            if previous != 'frozen' and mode not in ('frozen', 'shadow'):
                raise ValueError('freeze admission before changing writer')
        if mode == 'frozen':
            recover_outbound(con, now, source_id=source_id, in_transaction=True)
        ledger = cutover_ledger(con, source_id)
        if mode == 'legacy':
            if any(d['state'] in ('claimed','sending','uncertain') for d in ledger['deliveries']):
                raise ValueError('reconcile unknown external effects before restoring legacy sender')
            if ledger['messages'] and backfill != ledger['digest']:
                raise ValueError('incremental ledger backfill acknowledgement required')
        con.execute('INSERT INTO channel_cutovers VALUES(?,?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET mode=excluded.mode,high_water=excluded.high_water,backfill_digest=excluded.backfill_digest,updated_at=excluded.updated_at', (source_id,mode,ledger['high_water'],backfill,now.isoformat()))
        return dict(source_id=source_id,mode=mode,high_water=ledger['high_water'])


def _loop_guard(con, origin, context, now):
    """Read provenance and update its budget atomically with receive/processing claim."""
    import json
    from datetime import datetime, timedelta
    from .loops import Automation, guard
    from .models import digest
    scope = digest({k:v for k,v in origin.items() if k != 'message_id'})
    context = Automation() if context is None else context
    # Validate before using automation fields in a query.
    guard(context)
    state = con.execute('SELECT * FROM channel_loop_state WHERE scope_key=? AND correlation_id=?', (scope,context.correlation_id)).fetchone()
    echo = False
    for row in con.execute("SELECT a.provider_receipt FROM channel_delivery_attempts a JOIN channel_deliveries d ON d.id=a.delivery_id JOIN channel_sources s ON s.id=d.source_id WHERE d.kind='outbound' AND s.provider=? AND s.tenant_ref=? AND a.state IN ('provider_accepted','delivered')", (origin['provider'],origin['tenant_ref'])):
        try:
            receipt = json.loads(row[0] or '{}')
        except (ValueError, TypeError):
            continue
        if isinstance(receipt,dict) and receipt.get('conversation_ref') == origin['conversation_ref'] and receipt.get('message_id') == origin['message_id']:
            echo = True
            break
    status, provenance = guard(context,state=state,sent_echo=echo)
    if status in ('allowed','hop_limit','budget_exhausted','peer_disabled'):
        reset_at=(datetime.fromisoformat(now)+timedelta(seconds=300)).isoformat()
        # Exhaustion stays paused after the window. Only the control plane may reset.
        con.execute('INSERT INTO channel_loop_state(scope_key,correlation_id,turns,window_start,reset_at,paused,updated_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(scope_key,correlation_id) DO UPDATE SET turns=turns+excluded.turns,paused=MAX(paused,excluded.paused),updated_at=excluded.updated_at',
                    (scope,context.correlation_id,int(status=='allowed'),now,reset_at,int(status!='allowed'),now))
    return status, provenance


def reject_attachment(con, attachment_id, source_id, reason, now, holder, generation):
    import json
    from .models import canonical
    with store.immediate(con):
        _require_lease(con, source_id, holder, generation, now)
        row = con.execute("SELECT a.provider_attachment_ref,m.intake_receipt_id FROM channel_attachments a JOIN channel_messages m ON m.id=a.message_id JOIN channel_conversations c ON c.id=m.conversation_id WHERE a.id=? AND c.source_id=? AND a.status='queued' AND m.deleted_at IS NULL", (attachment_id,source_id)).fetchone()
        if row is None:
            raise ValueError('attachment no longer queued')
        content = con.execute('SELECT attachments FROM intake_content WHERE receipt_id=?', (row['intake_receipt_id'],)).fetchone()
        items = json.loads(content[0])
        for index, item in enumerate(items):
            if item.get('provider_attachment_ref', f'legacy-index:{index}') == row['provider_attachment_ref']:
                item['status'] = 'unavailable'
                item['note'] = reason
                break
        con.execute('UPDATE intake_content SET attachments=? WHERE receipt_id=?', (canonical(items),row['intake_receipt_id']))
        con.execute("UPDATE channel_attachments SET status='unavailable',updated_at=? WHERE id=?", (now,attachment_id))


def set_source_enabled(con, source_id, enabled):
    if type(enabled) is not bool:
        raise ValueError('invalid enabled state')
    with store.immediate(con):
        if con.execute('UPDATE channel_sources SET enabled=? WHERE id=?', (int(enabled), source_id)).rowcount != 1:
            raise ValueError('unknown source')


def matching_grant(con, draft, value, message, now, grant_id=None):
    from .models import canonical
    author = con.execute('SELECT * FROM channel_draft_authors WHERE draft_id=? AND version=?', (draft['draft_id'],draft['version'])).fetchone()
    if (author is None or author['role'] != 'orch' or author['identity'] != message['owner_orch']
            or author['payload_hash'] != draft['payload_hash']):
        return None
    rows = con.execute("SELECT * FROM channel_outbound_grants WHERE source_id=? AND state='active' ORDER BY created_at,grant_id", (value['source_id'],))
    for grant in rows:
        if (grant_id is not None and grant['grant_id'] != grant_id
                or grant['expires_at'] is not None and now.timestamp() >= grant['expires_at']
                or grant['route_id'] is not None and grant['route_id'] != value['route_id']
                or grant['target_json'] is not None and grant['target_json'] != canonical(value['target'])):
            continue
        return dict(grant)
    return None
