"""Immutable authenticated request provenance for native tasks."""
import hashlib
import json
import time
from . import store
SCHEMA = 'CREATE TABLE IF NOT EXISTS task_origins (\n task_id TEXT PRIMARY KEY,request_id INTEGER NOT NULL,origin TEXT NOT NULL,\n origin_digest TEXT NOT NULL,dispatch_key TEXT,args_digest TEXT NOT NULL,created_at REAL NOT NULL,\n UNIQUE(request_id,dispatch_key));'

def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()

def ensure(con):
    con.execute(SCHEMA)

def request_origin(con, rt, orch_thread, request_id, repo, *, active=True):
    if type(request_id) is not int or request_id < 1:
        raise ValueError('origin_request_id must be an authenticated interface request ID')
    row = con.execute('SELECT r.*,i.disabled FROM interface_requests r JOIN interfaces i ON i.id=r.interface_id WHERE r.id=?', (request_id,)).fetchone()
    if row is None or row['orch_id'] != orch_thread:
        raise ValueError('task origin must be an authenticated request owned by this Orch')
    if row['disabled'] or (active and (row['reply'] is not None or row['dismissed_at'] is not None)):
        raise ValueError('task origin is no longer active')
    return dict(source='interface_request', request_id=request_id, interface_id=row['interface_id'], conversation=row['conversation'], request_key=row['request_key'], repositories={repo: str(rt.repo_path(repo))}, request_body_sha256=hashlib.sha256(row['body'].encode()).hexdigest())

def replay(con, request_id, dispatch_key, args_digest):
    ensure(con)
    row = con.execute('SELECT * FROM task_origins WHERE request_id=? AND dispatch_key=?', (request_id, dispatch_key)).fetchone()
    if row is None:
        return None
    if row['args_digest'] != args_digest:
        raise ValueError('dispatch idempotency conflict')
    return store.get_task(con, row['task_id'])

def capture(con, task_id, origin, dispatch_key, args_digest):
    ensure(con)
    raw = json.dumps(origin, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    con.execute('INSERT INTO task_origins VALUES(?,?,?,?,?,?,?)', (task_id, origin['request_id'], raw, digest(origin), dispatch_key, args_digest, time.time()))

def for_task(con, task_id):
    ensure(con)
    row = con.execute('SELECT * FROM task_origins WHERE task_id=?', (task_id,)).fetchone()
    explicit = dict(json.loads(row['origin']), task_id=task_id, origin_digest=row['origin_digest']) if row else None
    from . import workflow_runs
    ordered = workflow_runs.task_origin(con, task_id)
    if ordered:
        origin = {k: ordered[k] for k in ('request_id', 'interface_id', 'conversation', 'request_key', 'repositories')}
        origin.update(source='workflow_run', task_id=task_id, run_id=ordered['run_id'])
        origin['origin_digest'] = digest({k: v for k, v in origin.items() if k != 'origin_digest'})
        if explicit and any((explicit[k] != origin[k] for k in ('request_id', 'interface_id', 'conversation', 'request_key'))):
            raise ValueError('conflicting task origin receipts')
        return explicit or origin
    return explicit
INCIDENT_SCHEMA = 'CREATE TABLE IF NOT EXISTS task_dispatch_incidents (\n id TEXT PRIMARY KEY,orch_thread TEXT NOT NULL,repo TEXT NOT NULL,args_digest TEXT NOT NULL,\n dispatch_key TEXT,origin_request_id INTEGER,code TEXT NOT NULL,state TEXT NOT NULL,\n task_id TEXT,created_at REAL NOT NULL,resolved_at REAL);'

def record_dispatch_incident(con, *, orch_thread, repo, args_digest, dispatch_key, origin_request_id, code):
    """Only safe classification/identity hashes; never retain request or task prose."""
    import uuid
    if code not in ('origin_missing', 'origin_invalid'):
        raise ValueError('unknown dispatch incident code')
    con.execute(INCIDENT_SCHEMA)
    identity = dict(orch_thread=orch_thread, repo=repo, args_digest=args_digest, dispatch_key=dispatch_key, origin_request_id=origin_request_id, code=code)
    identifier = uuid.uuid5(uuid.NAMESPACE_OID, digest(identity)).hex
    con.execute('INSERT OR IGNORE INTO task_dispatch_incidents VALUES(?,?,?,?,?,?,?,?,?,?,?)', (identifier, orch_thread, repo, args_digest, dispatch_key, origin_request_id, code, 'blocked', None, time.time(), None))
    return identifier

def list_dispatch_incidents(con, orch_thread=None):
    con.execute(INCIDENT_SCHEMA)
    return [dict(r) for r in con.execute('SELECT * FROM task_dispatch_incidents WHERE (? IS NULL OR orch_thread=?) ORDER BY created_at,id', (orch_thread, orch_thread))]

def resolve_dispatch_incidents(con, orch_thread, repo, args_digest, task_id):
    con.execute(INCIDENT_SCHEMA)
    con.execute("UPDATE task_dispatch_incidents SET state='resolved',task_id=?,resolved_at=? WHERE orch_thread=? AND repo=? AND args_digest=? AND state='blocked'", (task_id, time.time(), orch_thread, repo, args_digest))
