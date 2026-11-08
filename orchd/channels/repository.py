"""Channel storage, with foreign keys isolated from the legacy store connection."""
import sqlite3
from importlib.resources import files

from orchd import store


def _statements():
    sql = files('orchd.channels').joinpath('migrations/0001.sql').read_text(encoding='utf-8')
    statement = ''
    for line in sql.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            if not statement.lstrip().upper().startswith('PRAGMA'):
                yield statement
            statement = ''
    if statement.strip():
        raise ValueError('incomplete channel migration')


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
            if versions - {1}:
                raise ValueError('unsupported channel migration version')
            for statement in _statements():
                con.execute(statement)
            for kind, name, sql in objects:
                actual = con.execute('SELECT sql FROM sqlite_master WHERE type=? AND name=?', (kind, name)).fetchone()
                if actual is None or actual[0] != sql:
                    raise ValueError(f'incompatible channel schema: {name}')
            con.execute('INSERT OR IGNORE INTO orchd_channel_migrations VALUES (1)')
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


def _matching_route(con, source_id, conversation_ref, sender):
    import json
    candidates = con.execute("SELECT * FROM channel_routes WHERE source_id=? AND active=1 AND conversation_ref IN (?, '*')", (source_id, conversation_ref)).fetchall()
    matched = [r for r in candidates if not json.loads(r['sender_scope_json']) or sender in json.loads(r['sender_scope_json'])]
    if len(matched) > 1:
        raise ValueError('ambiguous route')
    return matched[0] if matched else None


def receive(con, source_id, event, adapter, clock, *, in_transaction=False):
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
        source = con.execute('SELECT * FROM channel_sources WHERE id=?', (source_id,)).fetchone()
        if source is None or not source['enabled'] or source['adapter_id'] != adapter:
            raise PermissionError('adapter not authorized for source')
        if (source['provider'],source['tenant_ref']) != (origin['provider'],origin['tenant_ref']):
            raise PermissionError('source origin mismatch')
        old = con.execute('SELECT * FROM channel_observations WHERE source_id=? AND transport_event_key=?', (source_id,event['transport_event_key'])).fetchone()
        if old:
            if old['content_hash'] != event_hash:
                raise ValueError('conflict')
            row = con.execute('SELECT intake_receipt_id,content_hash FROM channel_messages WHERE id=?',(old['message_id'],)).fetchone()
            if row['content_hash'] != content_hash:
                return {'status':'conflict','message_id':old['message_id'],'receipt_id':row['intake_receipt_id'],'review_required':True}
            state = con.execute('SELECT state FROM intake_events WHERE receipt_id=?', (row[0],)).fetchone()
            if suggestion and state and state[0] == 'received':
                from orchd import intake_notify
                _, appended = intake_bridge._add_suggestion(con, row[0], suggestion, adapter)
                if appended:
                    intake_notify.upgrade(con, row[0], intake_bridge._suggestion_urgency(suggestion))
            return {'status':'duplicate','message_id':old['message_id'],'receipt_id':row[0]}
        if con.execute("SELECT 1 FROM channel_messages m JOIN intake_content c ON c.receipt_id=m.intake_receipt_id WHERE m.origin_key=? AND m.deleted_at IS NOT NULL AND c.unavailable_reason='deleted' LIMIT 1", (origin_key,)).fetchone():
            raise ValueError('origin tombstoned')
        route = _matching_route(con, source_id, origin['conversation_ref'], event['sender_ref'])
        owner = route['owner_orch'] if route and route['mode'] != 'observer' else None
        existing = con.execute('SELECT * FROM channel_messages WHERE origin_key=? AND revision_key=?', (origin_key,event['revision_key'])).fetchone()
        if existing:
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
            con.execute('INSERT INTO channel_observations VALUES(?,?,?,?,?,?)',(source_id,event['transport_event_key'],origin_key,existing['id'],event_hash,now))
            return {'status':'duplicate','message_id':existing['id'],'receipt_id':existing['intake_receipt_id']}
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
        return {'status':'received','message_id':message_id,'receipt_id':receipt_id}


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



def tombstone(con, message_id, source_id, *, reason='deleted'):
    from datetime import datetime, timezone
    with store.immediate(con):
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
        lease = con.execute('SELECT * FROM channel_source_leases WHERE source_id=?', (source_id,)).fetchone()
        if not lease or lease['holder_id'] != holder or lease['generation'] != generation or lease['lease_until'] <= now:
            raise PermissionError('source lease lost')
        for receipt in receipts:
            if not con.execute('SELECT 1 FROM channel_observations o JOIN channel_messages m ON m.id=o.message_id WHERE o.source_id=? AND m.intake_receipt_id=?', (source_id, receipt)).fetchone():
                raise ValueError('receipt not durable for source')
        old = checkpoint(con, source_id, scope)
        backwards = old and ((int(old['committed_cursor']) > int(cursor)) if scope == 'history' else (old['committed_cursor'] > cursor))
        if backwards:
            raise ValueError('checkpoint cannot move backwards')
        con.execute('INSERT INTO channel_checkpoints VALUES(?,?,?,?,?,?) ON CONFLICT(source_id,scope) DO UPDATE SET committed_cursor=excluded.committed_cursor,window_end=excluded.window_end,receipt_set_hash=excluded.receipt_set_hash,updated_at=excluded.updated_at',
                    (source_id, scope, cursor, cursor, digest(sorted(receipts)), now))


def _require_lease(con, source_id, holder, generation, now):
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


def draft(con, message, payload):
    from orchd import intake
    source_row=source(con,message['source_id'])
    return intake.draft(con,{'receipt_id':message['intake_receipt_id'],
                             'provider':'email' if source_row['provider']=='gmail' else source_row['provider'],
                             'sender':payload['sender'],'target':payload['target'],
                             'source_ref':message['external_ref'],'body':payload['body'],
                             'summary':'Channel reply draft','expires_at':payload['expires_at']},actor='orch')


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
