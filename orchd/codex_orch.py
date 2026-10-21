"""Opt-in, read-only health for a locally managed Codex app-server Orch.

Registration captures the server's process identity, never a turn's exec PID.
Desktop and exec have no equivalent probe and remain unknown.
"""
import json
import math
import os
import stat
from pathlib import Path

from . import app_worker, store
from .app_transport import RPC


def capabilities(orch):
    kind = orch['kind']
    managed = kind == 'codex' and bool(dict(orch).get('codex_probe'))
    return dict(health_probe='codex_app_server' if managed else 'claude_jobs' if kind == 'claude'
                else 'unsupported', process_identity=managed, private_socket=managed, thread_status=managed)


def private_socket(endpoint):
    path = Path(endpoint)
    parent = path.parent
    directory, sock = parent.lstat(), path.lstat()
    if (not path.is_absolute() or parent.is_symlink() or not stat.S_ISDIR(directory.st_mode)
            or directory.st_uid != os.getuid() or stat.S_IMODE(directory.st_mode) != 0o700
            or not stat.S_ISSOCK(sock.st_mode) or sock.st_uid != os.getuid()):
        raise ValueError('Codex probe requires an owned socket in a private directory')


def probe(orch):
    raw = dict(orch).get('codex_probe')
    if not raw:
        return dict(state='unknown', reason='codex_probe_unsupported')
    try:
        spec = json.loads(raw)
        identity = spec['server_identity']
        data = json.loads(identity)
        if (spec['backend'] != 'app-server' or type(data['pid']) is not int or data['pid'] <= 0
                or type(data['birth']) not in (int, float) or not math.isfinite(data['birth']) or data['birth'] <= 0
                or type(data['pgid']) is not int or data['pgid'] <= 0
                or not isinstance(spec['endpoint'], str) or not spec['endpoint']
                or not spec['thread_id'] or spec['thread_id'] != (orch['session_id'] or orch['id'])):
            raise ValueError('invalid probe identity')
    except Exception:
        return dict(state='unknown', reason='codex_probe_invalid')
    try:
        if app_worker.process(identity) is None:
            return dict(state='dead', reason='codex_process_gone')
    except Exception:
        return dict(state='unknown', reason='codex_process_unavailable')
    try:
        private_socket(spec['endpoint'])
    except Exception:
        return dict(state='unknown', reason='codex_socket_unavailable')
    try:
        rpc = RPC(spec['endpoint'])
        try:
            thread = rpc.call('thread/read', {'threadId': spec['thread_id']}, timeout=3)['thread']
        finally:
            rpc.close()
    except Exception:
        return dict(state='unknown', reason='codex_socket_unavailable')
    try:
        if thread['id'] != spec['thread_id']:
            return dict(state='unknown', reason='codex_thread_mismatch')
        state = thread['status']['type']
        # A process exiting during the request cannot leave a stale alive verdict.
        if app_worker.process(identity) is None:
            return dict(state='dead', reason='codex_process_gone')
    except Exception:
        return dict(state='unknown', reason='codex_probe_invalid')
    if state == 'active':
        return dict(state='alive', reason='codex_thread_active')
    if state == 'idle':
        return dict(state='idle', reason='codex_thread_idle')
    return dict(state='unknown', reason='codex_thread_status_unsupported')


def register(con, orch_id, pid, endpoint):
    """Local launcher/Operator opt-in; no launch, delivery, restart or binding change."""
    orch = store.get_orch(con, orch_id)
    if orch is None or orch['kind'] != 'codex':
        raise ValueError('probe requires a registered Codex Orch')
    try:
        identity = app_worker.identity(pid)
    except Exception:
        raise ValueError('Codex process identity unavailable') from None
    spec = json.dumps(dict(backend='app-server', server_identity=identity,
                           endpoint=endpoint, thread_id=orch['session_id'] or orch['id']))
    health = probe({**dict(orch), 'codex_probe': spec})
    if health['state'] not in ('alive', 'idle'):
        raise ValueError(f"Codex probe registration failed: {health['reason']}")
    with store.immediate(con):
        if dict(store.get_orch(con, orch_id)) != dict(orch):
            raise ValueError('Orch changed during probe registration')
        con.execute('UPDATE orchs SET codex_probe=? WHERE id=?', (spec, orch_id))
    return health
