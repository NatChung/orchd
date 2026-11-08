"""Unified local channel service. No platform transport is imported here."""
from . import models, policy, repository


class ChannelService:
    def __init__(self, path, *, clock=None, retention=None, channel_home=None):
        from datetime import datetime, timezone
        from orchd import paths
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.retention = policy.retention_policies() if retention is None else policy.retention_policies({'retention': retention})
        self.channel_home = channel_home or paths.channel_home()
        self.con = repository.connect(path)
        try:
            self.enforce_retention(caller=policy.Caller(role='operator'))
        except BaseException:
            self.con.close()
            raise

    def close(self):
        self.con.close()

    def provision_source(self, source, *, caller):
        policy.require_operator(caller)
        import re
        from .secret_store import _REFERENCE
        allowed = {'id', 'host_id', 'provider', 'tenant_ref', 'account_ref', 'adapter_id', 'credential_refs', 'enabled'}
        if not isinstance(source, dict) or set(source) - allowed:
            raise ValueError('invalid source fields')
        for name in ('id', 'host_id', 'provider', 'account_ref', 'adapter_id'):
            if not isinstance(source.get(name), str) or not source[name]:
                raise ValueError('invalid source identity')
        if not re.fullmatch(r'[A-Za-z0-9_-]+', source['id']):
            raise ValueError('invalid source id')
        if not isinstance(source.get('tenant_ref', ''), str) or type(source.get('enabled', False)) is not bool:
            raise ValueError('invalid source settings')
        refs = source.get('credential_refs', {})
        if not isinstance(refs, dict) or not all(isinstance(k, str) and isinstance(v, str) and _REFERENCE.fullmatch(v)
                                                  and all(part not in ('', '.', '..') for part in v[len('os-store:'):].split('/'))
                                                  for k, v in refs.items()):
            raise ValueError('credential references required')
        if source['provider'] not in ('slack','line','gmail'):
            raise ValueError('unsupported provider')
        repository.provision(self.con, source)

    def receive(self, source_id, event, *, caller):
        policy.require_source(caller, source_id)
        if caller.role != 'adapter':
            raise PermissionError('adapter required')
        return repository.receive(self.con, source_id, models.validate_event(event), caller.identity, self.clock)



    def message(self, message_id, *, caller):
        import json
        row = repository.get_message(self.con, message_id)
        policy.require_message(caller, row)
        from datetime import datetime, timedelta
        days = self.retention.get(row['source_id'], {}).get('body_days', policy.DEFAULT_BODY_DAYS)
        if type(days) is not int or days < 0:
            raise ValueError('invalid retention policy')
        if not row['deleted_at'] and self.clock() >= datetime.fromisoformat(row['received_at']) + timedelta(days=days):
            self._tombstone(message_id, row['source_id'], reason='expired')
            row = repository.get_message(self.con, message_id)
        content = repository.content(self.con, row['intake_receipt_id'])
        content['attachments'] = json.loads(content['attachments'])
        content['metadata'] = json.loads(content.pop('channel_metadata'))
        if row['deleted_at']:
            content = {'status':'unavailable','text':None,'attachments':[],
                       'unavailable_reason':content['unavailable_reason'] or 'deleted'}
        return {'schema_version':1,'source_id':row['source_id'],'message_id':row['id'],'receipt_id':row['intake_receipt_id'],
                'status':content['status'],'last_synced_at':None,'freshness':'unknown','next_cursor':None,
                'trust':row['trust'],'content':content}

    def messages(self, source_id, *, caller, limit=100, cursor=''):
        policy.require_source(caller, source_id)
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError('invalid limit')
        owner = None if caller.role == 'operator' else (caller.owner_orch or caller.identity)
        rows=repository.list_messages(self.con, source_id,limit+1,cursor,owner)
        return {'schema_version':1,'source_id':source_id,'status':'partial','freshness':'unknown','last_synced_at':None,'messages':[self.message(r['id'],caller=caller) for r in rows[:limit]],'next_cursor':rows[limit-1]['id'] if len(rows)>limit else None}

    def search(self, source_id, query, *, caller, limit=100):
        if not isinstance(query,str) or not query.strip():
            raise ValueError('empty query')
        page=self.messages(source_id,caller=caller,limit=limit)
        page['messages']=[item for item in page['messages'] if query.casefold() in (item['content']['text'] or '').casefold()]
        page['status']='sync_required'
        return page

    def request_sync(self, source_id, request_key, scope, *, caller):
        policy.require_source(caller,source_id)
        if not request_key or not isinstance(scope,dict):
            raise ValueError('invalid sync request')
        job=repository.sync_request(self.con,source_id,request_key,scope,caller.identity)
        return {'schema_version':1,'source_id':source_id,'status':'queued','job_id':job['id']}

    def sync_status(self, job_id, *, caller):
        job=repository.sync_status(self.con,job_id)
        policy.require_source(caller,job['source_id'])
        return {'schema_version':1,**job}

    def request_attachment(self, attachment_id, *, caller):
        item=repository.attachment(self.con,attachment_id)
        policy.require_message(caller,item)
        if item['status'] == 'referenced':
            repository.queue_attachment(self.con,attachment_id)
            item['status']='queued'
        return {'schema_version':1,'source_id':item['source_id'],'attachment_id':attachment_id,'status':item['status']}

    def approve(self, *args, **kwargs):
        raise PermissionError('outbound disabled')

    def claim_outbound(self, *args, **kwargs):
        raise PermissionError('outbound disabled')


    def delete_message(self, message_id, *, caller):
        row=repository.get_message(self.con,message_id)
        if not isinstance(caller, policy.Caller) or caller.role != 'adapter':
            raise PermissionError('adapter required')
        authorized = False
        for source_id in caller.sources:
            source = repository.source(self.con, source_id)
            if source['enabled'] and source['adapter_id'] == caller.identity and repository.observed_by(self.con, message_id, source_id):
                authorized = True
                break
        if not authorized:
            raise PermissionError('adapter cannot delete this message')
        self._tombstone(message_id,row['source_id'])
        pending = len(repository.pending_deleted_attachments(self.con))
        return {'schema_version':1,'source_id':row['source_id'],'message_id':message_id,
                'status':'cleanup_pending' if pending else 'deleted'}



    def conversation(self, conversation_id, *, caller, cursor='', limit=100):
        if type(limit) is not int or not 1 <= limit <= 500 or not isinstance(cursor, str):
            raise ValueError('invalid conversation pagination')
        source_id=repository.conversation_source(self.con,conversation_id)
        policy.require_source(caller,source_id)
        owner = None if caller.role == 'operator' else (caller.owner_orch or caller.identity)
        ids=repository.conversation_messages(self.con,conversation_id,cursor,limit+1,owner)
        return {'schema_version':1,'source_id':source_id,'status':'partial' if len(ids)>limit else 'available',
                'last_synced_at':None,'freshness':'unknown','next_cursor':ids[limit-1] if len(ids)>limit else None,
                'messages':[self.message(mid,caller=caller) for mid in ids[:limit]]}

    def read_attachment(self, attachment_id, *, caller):
        item=repository.attachment(self.con,attachment_id)
        policy.require_message(caller,item)
        status = 'unavailable' if item['status'] in ('deleted', 'unavailable') else 'adapter_unavailable'
        return {'schema_version':1,'source_id':item['source_id'],'attachment_id':attachment_id,'status':status}

    def draft_reply(self, message_id, payload, *, caller):
        row=repository.get_message(self.con,message_id)
        policy.require_message(caller,row)
        if row['deleted_at']:
            raise ValueError('cannot draft from deleted message')
        if not isinstance(payload,dict) or set(payload) != {'sender','target','body','expires_at'}:
            raise ValueError('invalid draft payload')
        return repository.draft(self.con,row,payload)

    def routes(self, *, caller):
        policy.require_operator(caller)
        return repository.routes(self.con)

    def apply_routes(self, routes, *, caller):
        from . import routing
        policy.require_operator(caller)
        if not isinstance(routes,list):
            raise ValueError('routes must be a list')
        routing.validate_routes(routes)
        repository.apply_routes(self.con,routes)
        return {'status':'applied','count':len(routes)}


    def link_legacy_intake(self, source_id, receipt_id, *, caller):
        policy.require_operator(caller)
        return repository.link_legacy_intake(self.con,source_id,receipt_id)

    def enforce_retention(self, *, caller):
        from datetime import datetime, timedelta
        policy.require_operator(caller)
        removed=0
        for row in repository.retention_candidates(self.con):
            days=self.retention.get(row['source_id'],{}).get('body_days',policy.DEFAULT_BODY_DAYS)
            if type(days) is not int or days < 0:
                raise ValueError('invalid retention policy')
            if self.clock() >= datetime.fromisoformat(row['received_at']) + timedelta(days=days):
                self._tombstone(row['id'],row['source_id'],reason='expired')
                removed += 1
        self._cleanup_deleted_storage()
        pending = len(repository.pending_deleted_attachments(self.con))
        pending_backups = self._cleanup_backups()
        return {'status':'cleanup_pending' if pending or pending_backups else 'complete','tombstoned':removed,
                'pending_attachments':pending,'pending_backups':pending_backups}

    def _tombstone(self, message_id, source_id, *, reason='deleted'):
        repository.tombstone(self.con, message_id, source_id, reason=reason)
        self._cleanup_deleted_storage()

    def _cleanup_deleted_storage(self):
        import re
        from pathlib import Path
        root = Path(self.channel_home).absolute()
        for item in repository.pending_deleted_attachments(self.con):
            key, source_id = item['storage_key'], item['source_id']
            if not re.fullmatch(r'[0-9a-f]{64}', key) or not re.fullmatch(r'[A-Za-z0-9_-]+', source_id):
                continue
            provider = {'gmail': 'email', 'slack': 'slack', 'line': 'line'}.get(item['provider'])
            if provider is None:
                continue
            target = root / provider / 'state' / source_id / 'content' / key
            current = root
            unsafe = current.is_symlink()
            for part in (provider, 'state', source_id, 'content', key):
                current = current / part
                unsafe = unsafe or current.is_symlink()
            if unsafe:
                continue
            try:
                target.unlink(missing_ok=True)
            except OSError:
                continue
            repository.clear_deleted_storage(self.con, item['id'], key)

    def _cleanup_backups(self):
        import re
        from pathlib import Path
        database = Path(self.con.execute('PRAGMA database_list').fetchone()[2])
        root = database.parent / 'backups'
        if root.is_symlink():
            return 1
        if not root.is_dir():
            return 0
        backup_days = min([policy.MAX_BACKUP_DAYS] + [item.get('backup_days', policy.MAX_BACKUP_DAYS) for item in self.retention.values()])
        cutoff = self.clock().timestamp() - backup_days * 86400
        pending = 0
        for path in root.iterdir():
            if not re.fullmatch(r'orchd-\d{8}-\d{6}-channel-pre-(?:intake|question)-reconcile\.db', path.name):
                continue
            if path.is_symlink() or not path.is_file():
                pending += 1
                continue
            try:
                if path.stat().st_mtime <= cutoff:
                    path.unlink()
            except OSError:
                pending += 1
        return pending


def legacy_interface(con, operation, *, runtime=None, actor=None, payload=None):
    """Compatibility facade; existing interface tables retain request/reply authority."""
    from orchd import interface
    payload = dict(payload or {})
    if operation == 'list':
        return interface.list_interfaces(con)
    if operation == 'rebind':
        return interface.rebind(con, runtime, **payload)
    if operation == 'submit':
        if set(payload) != {'name', 'token', 'conversation', 'request_key', 'text'}:
            raise ValueError('invalid legacy interface submit fields')
        if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='channel_sources'").fetchone():
            return interface.submit(con, runtime, **payload)
        configured = con.execute('SELECT id,enabled FROM channel_sources WHERE adapter_id=?', (payload['name'],)).fetchall()
        sources = [row for row in configured if row['enabled']]
        if not sources:
            if configured:
                raise PermissionError('channel source disabled')
            return interface.submit(con, runtime, **payload)
        if len(sources) != 1:
            raise ValueError('ambiguous legacy interface source mapping')
        dedicated = repository.connect(con.execute('PRAGMA database_list').fetchone()[2])
        try:
            source_id = sources[0][0]
            return interface.submit(dedicated, runtime, **payload,
                                    _receipt_hook=lambda db, rid, interface_row: repository.link_legacy_interface(db, source_id, rid, interface_row))
        finally:
            dedicated.close()
    if operation == 'requests':
        items = interface.requests(con, actor, **payload)
        if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='channel_legacy_refs'").fetchone():
            return items
        result = []
        caller = policy.caller_for_identity(actor)
        channel = ChannelService(con.execute('PRAGMA database_list').fetchone()[2], retention=policy.retention_policies())
        try:
            for item in items:
                link = con.execute("SELECT new_id FROM channel_legacy_refs WHERE system='interface' AND old_id=? AND new_kind='message'", (str(item['id']),)).fetchone()
                if link:
                    try:
                        view = channel.message(link[0], caller=caller)
                    except PermissionError:
                        continue
                    item['body'] = view['content']['text'] or ''
                    if view['content']['status'] == 'unavailable':
                        item['interim'] = '' if item['interim'] is not None else None
                result.append(item)
        finally:
            channel.close()
        return result
    if operation in ('reply','dismiss'):
        from orchd import workflow_runs
        workflow_runs.guard_interface_reply(con, payload.get('request_id'), actor)
        if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='channel_legacy_refs'").fetchone():
            link = con.execute("SELECT new_id FROM channel_legacy_refs WHERE system='interface' AND old_id=? AND new_kind='message'", (str(payload.get('request_id')),)).fetchone()
            if link:
                channel = ChannelService(con.execute('PRAGMA database_list').fetchone()[2], retention=policy.retention_policies())
                try:
                    channel.message(link[0], caller=policy.caller_for_identity(actor))
                finally:
                    channel.close()
        return getattr(interface,operation)(con, actor, **payload)
    if operation == 'fetch' and con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='channel_sources'").fetchone():
        source = con.execute('SELECT 1 FROM channel_sources WHERE adapter_id=?', (payload.get('name'),)).fetchone()
        if source:
            raise ValueError('channel outbound is disabled')
    if operation in ('register','fetch','ack','deregister'):
        return getattr(interface,operation)(con, **payload)
    raise ValueError('unknown legacy interface operation')


def legacy_intake_submit(con, payload, *, producer, producers):
    """Preserve the legacy receipt and deduplication authority during transition."""
    from orchd import intake
    event = payload if isinstance(payload, dict) else {}
    intake.check_producer(producers, producer, event.get('provider'), event.get('account'))
    if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='channel_sources'").fetchone():
        return intake.submit(con, payload, producer=producer, producers=producers)
    provider = 'gmail' if event.get('provider') == 'email' else event.get('provider')
    configured = con.execute('SELECT id,adapter_id,enabled FROM channel_sources WHERE provider=? AND account_ref=?',
                             (provider, event.get('account'))).fetchall()
    sources = [row for row in configured if row['adapter_id'] == producer and row['enabled']]
    if not sources:
        if configured:
            raise PermissionError('legacy producer is not authorized for configured source')
        return intake.submit(con, payload, producer=producer, producers=producers)
    if len(sources) != 1:
        raise ValueError('ambiguous legacy source mapping')
    if event.get('owner_orch') is not None:
        raise ValueError('legacy adapter cannot set channel owner')
    dbpath = con.execute('PRAGMA database_list').fetchone()[2]
    dedicated = repository.connect(dbpath)
    try:
        source_id = sources[0][0]
        return intake.submit(dedicated, payload, producer=producer, producers=producers,
                             _receipt_hook=lambda db, receipt: repository.link_legacy_intake(db, source_id, receipt, in_transaction=True))
    finally:
        dedicated.close()


def require_legacy_access(con, receipt_id, caller):
    """Enforce channel source/owner grants and retention on a legacy receipt."""
    if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='channel_messages'").fetchone():
        return
    row=con.execute('SELECT id FROM channel_messages WHERE intake_receipt_id=?',(receipt_id,)).fetchone()
    if row is None:
        return
    channel=ChannelService(con.execute('PRAGMA database_list').fetchone()[2],retention=policy.retention_policies())
    try:
        channel.message(row[0],caller=caller)
    finally:
        channel.close()
