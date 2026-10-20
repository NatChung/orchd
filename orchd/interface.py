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


def migrate_owner(con, old_id, new_id):
    """Restart hook: caller holds interface ownership delivery lock and a transaction.

    Move every registration, including inactive ones. Request destinations and
    credentials stay immutable; only the processing owner and wake state change.
    """
    _owner(con, new_id)
    moved = con.execute('UPDATE interfaces SET orch_id=? WHERE orch_id=?', (new_id, old_id)).rowcount
    # A wake accepted by the old session does not notify its replacement.
    con.execute('UPDATE interface_requests SET wake_at=NULL,wake_error=NULL,wake_attempts=0,wake_due_at=0 '
                'WHERE orch_id=? AND reply IS NULL', (old_id,))
    con.execute('UPDATE interface_requests SET orch_id=? WHERE orch_id=?', (new_id, old_id))
    return moved


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
    with store.task_delivery(con, ['interface-owner', f'interface-wake:{request_id}']):
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


def _kind(value):
    if value not in ('interim', 'final'):
        raise ValueError("kind must be 'interim' or 'final'")
    return value


def requests(con, orch_id, *, after_id=0, limit=100):
    """Requests still awaiting a final reply; an interim reply does not remove one."""
    _page(after_id, limit)
    _owner(con, orch_id)
    return [dict(row, trust='external_untrusted') for row in con.execute(
        'SELECT id,interface_id,conversation,body,created_at,interim FROM interface_requests WHERE orch_id=? AND reply IS NULL AND id>? ORDER BY id LIMIT ?',
        (orch_id, after_id, limit))]


def reply(con, orch_id, *, request_id, text, kind='final'):
    """Record the optional interim (at most one, immutable) or the single final reply.

    Same-kind same-text retries are idempotent; different text conflicts. An interim
    is refused once a final exists, unless it repeats the recorded interim text.
    """
    _request_id(request_id)
    _text(text)
    _kind(kind)
    with store.immediate(con):
        _owner(con, orch_id)
        row = con.execute('SELECT * FROM interface_requests WHERE id=? AND orch_id=?', (request_id, orch_id)).fetchone()
        if row is None:
            raise ValueError('request not owned by this Orch')
        if kind == 'final':
            if row['reply'] is not None and row['reply'] != text:
                raise ValueError('reply already recorded with different content')
            con.execute('UPDATE interface_requests SET reply=? WHERE id=?', (text, request_id))
        elif row['interim'] is not None and row['interim'] != text:
            raise ValueError('interim reply already recorded with different content')
        elif row['interim'] is None:
            if row['reply'] is not None:
                raise ValueError('interim reply refused: final reply already recorded')
            # The owner is acting on the request: its pending wake is moot. migrate_owner
            # clears wake_at for unanswered requests, so a replacement owner is still woken.
            con.execute('UPDATE interface_requests SET interim=?,wake_at=COALESCE(wake_at,?),wake_error=NULL WHERE id=?',
                        (text, time.time(), request_id))
    # No recipient argument, Desktop calls or transport: route is frozen at submission.
    return dict(request_id=request_id, interface_id=row['interface_id'], conversation=row['conversation'])


def _kinds(kinds):
    if kinds is None:
        return ('final',)
    if (not isinstance(kinds, list) or not kinds or len(set(kinds)) != len(kinds)
            or not set(kinds) <= {'interim', 'final'} or 'final' not in kinds):
        raise ValueError("kinds must be ['final'] or ['interim','final']")
    return tuple(kinds)


def fetch(con, *, name, token, conversation, after_id=0, limit=100, kinds=None):
    """Unacked replies by request id; an interim precedes its final.

    Without `kinds` only finals are returned, in the original row shape (no `kind` field).
    An adapter declares interim support with kinds=['interim','final']. `limit`
    counts requests, so one request may yield two rows.
    """
    _page(after_id, limit)
    _text(conversation)
    legacy = kinds is None
    kinds = _kinds(kinds)
    with store.immediate(con):
        row = _auth(con, name, token)
        # Polling keeps an active lease alive, but cannot reactivate an expired lease.
        con.execute('UPDATE interfaces SET expires_at=? WHERE id=? AND disabled=0 AND expires_at>?',
                    (time.time() + row['idle_seconds'], row['id'], time.time()))
        want_interim = 'interim' in kinds
        result = []
        for r in con.execute(
                'SELECT id,reply,ack_at,interim,interim_ack_at FROM interface_requests WHERE interface_id=? AND conversation=? '
                'AND id>? AND ((reply IS NOT NULL AND ack_at IS NULL) OR (? AND interim IS NOT NULL AND interim_ack_at IS NULL)) '
                'ORDER BY id LIMIT ?', (row['id'], conversation, after_id, int(want_interim), limit)):
            if want_interim and r['interim'] is not None and r['interim_ack_at'] is None:
                result.append(dict(request_id=r['id'], conversation=conversation, kind='interim', reply=r['interim']))
            if r['reply'] is not None and r['ack_at'] is None:
                # Legacy callers keep the original row shape (no kind field).
                item = dict(request_id=r['id'], conversation=conversation, reply=r['reply'])
                result.append(item if legacy else dict(item, kind='final'))
    return result


def ack(con, *, name, token, conversation, request_id, kind='final'):
    _request_id(request_id)
    _text(conversation)
    column, text = ('ack_at', 'reply') if _kind(kind) == 'final' else ('interim_ack_at', 'interim')
    with store.immediate(con):
        row = _auth(con, name, token)
        updated = con.execute(f'UPDATE interface_requests SET {column}=COALESCE({column},?) WHERE id=? AND interface_id=? AND conversation=? AND {text} IS NOT NULL',
                              (time.time(), request_id, row['id'], conversation)).rowcount
        if not updated:
            raise ValueError(f'no {kind} reply in this interface/conversation')
    return dict(acked=True, request_id=request_id) if kind == 'final' else dict(acked=True, request_id=request_id, kind=kind)


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
