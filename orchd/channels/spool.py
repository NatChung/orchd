"""Private, source-bound durable ingress queue; not an edge service or authority DB."""
import json
import sqlite3
from datetime import datetime, timezone
from .models import canonical, digest

class Spool:
    def __init__(self, path, source_id, *, clock=None, max_events=10000):
        from pathlib import Path
        path=Path(path)
        from orchd.channel_layout import _check_path, _make_private
        _check_path(path)
        if path.is_symlink():
            raise ValueError('unsafe spool path')
        path.parent.mkdir(parents=True,exist_ok=True)
        self.con=sqlite3.connect(path, isolation_level=None)
        _make_private(path)
        import os
        if os.name != 'nt': path.chmod(0o600)
        self.con.row_factory=sqlite3.Row
        self.con.execute('PRAGMA secure_delete=ON')
        self.con.execute('PRAGMA journal_mode=DELETE')
        self.con.executescript('CREATE TABLE IF NOT EXISTS binding(source TEXT PRIMARY KEY); CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,event_key TEXT UNIQUE NOT NULL,event_hash TEXT NOT NULL,payload TEXT NOT NULL,received_at TEXT NOT NULL); CREATE TABLE IF NOT EXISTS cursor(seq INTEGER NOT NULL); INSERT INTO cursor SELECT 0 WHERE NOT EXISTS(SELECT 1 FROM cursor); CREATE TABLE IF NOT EXISTS tombstones(origin TEXT PRIMARY KEY);')
        old=self.con.execute('SELECT source FROM binding').fetchone()
        if old and old[0]!=source_id:
            self.con.close(); raise PermissionError('spool source mismatch')
        self.con.execute('INSERT OR IGNORE INTO binding VALUES(?)',(source_id,))
        self.clock=clock or (lambda:datetime.now(timezone.utc).isoformat())
        self.max_events=max_events
        self.source_id=source_id
    def close(self):
        self.con.close()
    def append(self, events):
        from orchd import store
        with store.immediate(self.con):
            for event in events:
                key=event['transport_event_key']; hashed=digest(event)
                old=self.con.execute('SELECT event_hash FROM events WHERE event_key=?',(key,)).fetchone()
                if old:
                    if old[0]!=hashed: raise ValueError('spool event conflict')
                    continue
                if self.con.execute('SELECT count(*) FROM events WHERE seq>(SELECT seq FROM cursor)').fetchone()[0]>=self.max_events:
                    raise ValueError('retention_gap: spool capacity reached')
                origin=digest(event['origin']) if 'origin' in event else None
                deleted=event.get('content',{}).get('unavailable_reason')=='deleted'
                if deleted:
                    self.con.execute('INSERT OR IGNORE INTO tombstones VALUES(?)',(origin,))
                    for row in self.con.execute('SELECT seq,payload FROM events').fetchall():
                        previous=json.loads(row['payload'])
                        if previous.get('origin')==event['origin']:
                            previous['content']={'status':'unavailable','text':None,'attachments':[],'unavailable_reason':'deleted'}
                            self.con.execute('UPDATE events SET payload=? WHERE seq=?',(canonical(previous),row['seq']))
                if origin and self.con.execute('SELECT 1 FROM tombstones WHERE origin=?',(origin,)).fetchone():
                    event=dict(event,content={'status':'unavailable','text':None,'attachments':[],'unavailable_reason':'deleted'})
                self.con.execute('INSERT INTO events(event_key,event_hash,payload,received_at) VALUES(?,?,?,?)',(key,hashed,canonical(event),self.clock()))
    def pending(self, *, limit=100):
        if type(limit) is not int or not 1<=limit<=500: raise ValueError('invalid page size')
        return [{'seq':r['seq'],'event':json.loads(r['payload'])} for r in self.con.execute('SELECT seq,payload FROM events WHERE seq>(SELECT seq FROM cursor) ORDER BY seq LIMIT ?',(limit,))]
    def ack(self, seq):
        from orchd import store
        with store.immediate(self.con):
            current=self.con.execute('SELECT seq FROM cursor').fetchone()[0]
            next_row=self.con.execute('SELECT min(seq) FROM events WHERE seq>?',(current,)).fetchone()[0]
            if seq<=current: return
            if seq!=next_row: raise ValueError('noncontiguous spool ack')
            # Preserve the key/hash ledger, erase acknowledged private content.
            self.con.execute("UPDATE events SET payload='{}' WHERE seq=?",(seq,))
            self.con.execute('UPDATE cursor SET seq=?',(seq,))
