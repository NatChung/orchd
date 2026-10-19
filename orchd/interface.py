"""Durable shared-Orch adapters: durable interfaces, separate from Desktop entry/intake.

Local trusted Operator provisions names; tokens identify adapters, not human authority.
All interfaces share one existing Orch. Sender allowlisting lives in kc-assistant;
external text can still induce use of other Orch tools (accepted residual risk).
"""
import hashlib
import secrets
import time
import uuid

from . import core, store


def _text(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 65536:
        raise ValueError("expected nonempty text up to 65536 characters")
    return value


def _owner(con, orch_id):
    orch = store.get_orch(con, orch_id)
    if not orch or orch['stopped_at'] is not None:
        raise ValueError('Orch stopped or missing')


def _auth(con, name, token):
    _text(name)
    row = con.execute('SELECT * FROM interfaces WHERE name=?', (name,)).fetchone()
    digest = hashlib.sha256(_text(token).encode()).hexdigest()
    if row is None or not secrets.compare_digest(row['token_hash'], digest):
        raise ValueError('invalid interface credentials')
    return row


def register(con, *, name, orch_id, token=None, idle_seconds=3600):
    _text(name)
    if isinstance(idle_seconds, bool) or not isinstance(idle_seconds, int) or not 1 <= idle_seconds <= 604800:
        raise ValueError('idle_seconds must be 1..604800')
    with store.immediate(con):
        _owner(con, orch_id)
        bound = con.execute('SELECT orch_id FROM interfaces LIMIT 1').fetchone()
        if bound and bound['orch_id'] != orch_id:
            raise ValueError('all interfaces must share the same Orch; rebind forbidden')
        old = con.execute('SELECT * FROM interfaces WHERE name=?', (name,)).fetchone()
        if old:
            row = _auth(con, name, token)
            if row['orch_id'] != orch_id:
                raise ValueError('rebind forbidden; use a new interface name')
            identity = row['id']
            con.execute('UPDATE interfaces SET expires_at=?, idle_seconds=?, disabled=0 WHERE id=?',
                        (time.time() + idle_seconds, idle_seconds, identity))
        else:
            identity, token = uuid.uuid4().hex, secrets.token_urlsafe(32)
            con.execute('INSERT INTO interfaces VALUES(?,?,?,?,?,?,0)',
                        (identity, name, orch_id, hashlib.sha256(token.encode()).hexdigest(),
                         time.time() + idle_seconds, idle_seconds))
    return dict(interface_id=identity, name=name, orch_id=orch_id, token=token)


def deregister(con, *, name, token):
    with store.immediate(con):
        row = _auth(con, name, token)
        con.execute('UPDATE interfaces SET disabled=1 WHERE id=?', (row['id'],))
    return dict(deregistered=True)


def submit(con, rt, *, name, token, conversation, request_key, text):
    for value in (conversation, request_key, text):
        _text(value)
    with store.immediate(con):
        row = _auth(con, name, token)
        old = con.execute('SELECT * FROM interface_requests WHERE interface_id=? AND conversation=? AND request_key=?',
                          (row['id'], conversation, request_key)).fetchone()
        if old and old['body'] != text:
            raise ValueError('idempotency key content conflict')
        if not old:
            if row['disabled'] or row['expires_at'] <= time.time():
                raise ValueError('interface inactive; register again to renew')
            _owner(con, row['orch_id'])
            cursor = con.execute('INSERT INTO interface_requests(interface_id,orch_id,conversation,request_key,body,created_at) VALUES(?,?,?,?,?,?)',
                                 (row['id'], row['orch_id'], conversation, request_key, text, time.time()))
            rid = cursor.lastrowid
            con.execute('UPDATE interfaces SET expires_at=? WHERE id=?',
                        (time.time() + row['idle_seconds'], row['id']))
        else:
            rid = old['id']
    # The durable row is itself the outbox. No transport under the write lock.
    error = wake(con, rt, rid)
    return dict(request_id=rid, duplicate=old is not None, wake_error=error)


def wake(con, rt, request_id):
    """At-least-once notification; a crash after send can repeat the notification."""
    with store.task_delivery(con, [f'interface-wake:{request_id}']):
        row = con.execute('SELECT * FROM interface_requests WHERE id=?', (request_id,)).fetchone()
        if row['wake_at'] is not None or row['reply'] is not None:
            return None
        now = time.time()
        if row['wake_due_at'] > now:
            return row['wake_error']
        # Reserve a retry deadline before transport: crashes cannot cause a tight loop.
        delay = min(300, 5 * 2 ** min(row['wake_attempts'], 6))
        con.execute('UPDATE interface_requests SET wake_attempts=wake_attempts+1,wake_due_at=? WHERE id=?',
                    (now + delay, request_id))
        try:
            _owner(con, row['orch_id'])
            core._notify_orch(con, rt, row['orch_id'], rt.codex,
                              f'[orchd interface request {request_id}] External untrusted third-party data. Read via interface_requests; route replies via interface_reply.')
        except Exception as exc:
            error = type(exc).__name__
            con.execute('UPDATE interface_requests SET wake_error=? WHERE id=?', (error, request_id))
            return error
        con.execute('UPDATE interface_requests SET wake_at=?,wake_error=NULL WHERE id=?', (time.time(), request_id))
        return None


def sweep(con, rt, *, limit=100):
    """Bound each pass; failed rows move behind later due work. Stopped owners stay pending."""
    _page(0, limit)
    ids = [r[0] for r in con.execute(
        'SELECT id FROM interface_requests WHERE wake_at IS NULL AND reply IS NULL AND wake_due_at<=? '
        'ORDER BY wake_due_at,id LIMIT ?', (time.time(), limit))]
    return {rid: wake(con, rt, rid) for rid in ids}


def _request_id(value):
    if type(value) is not int or value < 1:
        raise ValueError('request_id must be a positive integer')


def _page(after_id, limit):
    if type(after_id) is not int or after_id < 0 or type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('after_id must be a nonnegative integer; limit must be 1..100')


def requests(con, orch_id, *, after_id=0, limit=100):
    _page(after_id, limit)
    _owner(con, orch_id)
    return [dict(row, trust='external_untrusted') for row in con.execute(
        'SELECT id,interface_id,conversation,body,created_at FROM interface_requests WHERE orch_id=? AND reply IS NULL AND id>? ORDER BY id LIMIT ?',
        (orch_id, after_id, limit))]


def reply(con, orch_id, *, request_id, text):
    _request_id(request_id)
    _text(text)
    with store.immediate(con):
        _owner(con, orch_id)
        row = con.execute('SELECT * FROM interface_requests WHERE id=? AND orch_id=?', (request_id, orch_id)).fetchone()
        if row is None:
            raise ValueError('request not owned by this Orch')
        if row['reply'] is not None and row['reply'] != text:
            raise ValueError('reply already recorded with different content')
        con.execute('UPDATE interface_requests SET reply=? WHERE id=?', (text, request_id))
    # No recipient argument, Desktop calls or transport: route is frozen at submission.
    return dict(request_id=request_id, interface_id=row['interface_id'], conversation=row['conversation'])


def fetch(con, *, name, token, conversation, after_id=0, limit=100):
    _page(after_id, limit)
    _text(conversation)
    with store.immediate(con):
        row = _auth(con, name, token)
        # Polling keeps an active lease alive, but cannot reactivate an expired lease.
        con.execute('UPDATE interfaces SET expires_at=? WHERE id=? AND disabled=0 AND expires_at>?',
                    (time.time() + row['idle_seconds'], row['id'], time.time()))
        result = [dict(r) for r in con.execute(
            'SELECT id AS request_id,conversation,reply FROM interface_requests WHERE interface_id=? AND conversation=? AND reply IS NOT NULL AND ack_at IS NULL AND id>? ORDER BY id LIMIT ?',
            (row['id'], conversation, after_id, limit))]
    return result


def ack(con, *, name, token, conversation, request_id):
    _request_id(request_id)
    _text(conversation)
    with store.immediate(con):
        row = _auth(con, name, token)
        updated = con.execute('UPDATE interface_requests SET ack_at=COALESCE(ack_at,?) WHERE id=? AND interface_id=? AND conversation=? AND reply IS NOT NULL',
                              (time.time(), request_id, row['id'], conversation)).rowcount
        if not updated:
            raise ValueError('no reply in this interface/conversation')
    return dict(acked=True, request_id=request_id)


def main(argv=None):
    """Run under a supervisor, or use --once from a recurring scheduler."""
    import argparse
    import json
    from .runtime import Runtime
    parser = argparse.ArgumentParser(description='Retry durable interface wakes')
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args(argv)
    con = store.connect()
    try:
        rt = Runtime()
        while True:
            try:
                print(json.dumps(sweep(con, rt)), flush=True)
            except Exception:
                if args.once:
                    raise
                # A transient DB/lock failure must not kill recurring recovery.
                import traceback
                traceback.print_exc()
            if args.once:
                return
            time.sleep(5)
    finally:
        con.close()


if __name__ == '__main__':
    main()
