"""Read-only local projections. Never construct a migrating ChannelService."""
from pathlib import Path
from datetime import datetime, timezone
from orchd import channel_layout, store


def status(path=None, *, home=None, env=None):
    root, state = channel_layout._locations(home, env)
    result = dict(schema_version=1, layout_version=None, sources=[], status='unavailable')
    journal = state / 'layout.sqlite3'
    if journal.is_file():
        from orchd.stats import open_snapshot
        with open_snapshot(journal) as snap:
            row = snap.con.execute("SELECT layout_version FROM migrations WHERE root=? AND status='complete' ORDER BY rowid DESC LIMIT 1", (str(root),)).fetchone()
            if row:
                result['layout_version'] = row[0]
    path = Path(path or store.home() / 'orchd.db')
    if not path.is_file():
        return result
    con = store.connect_readonly(path)
    try:
        if not con.execute("SELECT 1 FROM sqlite_master WHERE name='channel_sources'").fetchone():
            return result
        now = datetime.now(timezone.utc)
        for source in con.execute('SELECT id,adapter_id,enabled FROM channel_sources ORDER BY id'):
            sid = source['id']
            lease = con.execute('SELECT holder_id,lease_until FROM channel_source_leases WHERE source_id=?', (sid,)).fetchone()
            checkpoints = {r['scope']: dict(r) for r in con.execute('SELECT * FROM channel_checkpoints WHERE source_id=?', (sid,))}
            heartbeat = checkpoints.get('adapter_heartbeat')
            online = bool(heartbeat and (now - datetime.fromisoformat(heartbeat['committed_cursor'])).total_seconds() <= 300)
            states = {r[0]: r[1] for r in con.execute('SELECT state,COUNT(*) FROM channel_deliveries WHERE source_id=? GROUP BY state', (sid,))}
            health = checkpoints.get('adapter_health', {}).get('receipt_set_hash')
            mode = None
            if con.execute("SELECT 1 FROM sqlite_master WHERE name='channel_cutovers'").fetchone():
                row = con.execute('SELECT mode FROM channel_cutovers WHERE source_id=?', (sid,)).fetchone()
                mode = row[0] if row else 'compatibility'
            result['sources'].append(dict(source_id=sid,adapter_owner=lease['holder_id'] if lease else source['adapter_id'],
                admission=mode,
                enabled=bool(source['enabled']),last_success=checkpoints.get('mail',{}).get('updated_at'),
                freshness='fresh' if online else 'stale',pending=sum(states.get(s,0) for s in ('queued','claimed')),
                uncertain=states.get('uncertain',0),retention_gap=health == 'degraded',credential_error=health == 'credential_error',
                credential_status='unavailable' if source['enabled'] and not health else 'reported' if health else 'disabled'))
        result['status'] = 'available'
        return result
    finally:
        con.close()
