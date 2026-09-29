"""Bounded process-stop evidence, never proof that arbitrary descendants cannot escape.

Call capture before provider stop. Persist its JSON value before sending signals.
owned_group=True requires an explicitly recorded start_new_session launch. Observed
ancestry/session membership is not an OS sandbox or proof about shared services.
"""
import os
import signal
import subprocess
import time


LIMITATION = ('ps snapshots can miss double-fork/setsid escapes and external service writers; '
              'PID start timestamps have platform resolution; operator confirmation is required')


def inventory():
    """Current UID only; no argv, environment, or command contents are collected."""
    result = subprocess.run(['ps', '-axo', 'pid=,ppid=,pgid=,uid=,lstart=,stat='],
                            capture_output=True, text=True, timeout=5,
                            env=dict(os.environ, LC_ALL='C'))
    if result.returncode:
        raise RuntimeError('process inventory unavailable')
    rows = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) != 10:
            raise RuntimeError('process inventory format unknown')
        pid, ppid, pgid, uid = map(int, parts[:4])
        if uid != os.getuid():
            continue
        try:
            sid = os.getsid(pid)
        except ProcessLookupError:
            continue
        except PermissionError as error:
            raise RuntimeError('cannot inspect process session') from error
        rows[pid] = dict(pid=pid, ppid=ppid, pgid=pgid, sid=sid,
                         start=' '.join(parts[4:9]), state=parts[9])
    return rows


def key(row):
    return str(row['pid']) + ':' + row['start']


def live(row):
    return row is not None and not row['state'].startswith('Z')


def descendants(rows, root_pid):
    found = {root_pid}
    while True:
        more = {pid for pid, row in rows.items() if row['ppid'] in found}
        if more <= found:
            return found
        found |= more


def capture(pid, start, owned_group=False):
    rows = inventory()
    root = rows.get(pid)
    if not live(root) or root['start'] != start:
        raise RuntimeError('root PID identity unavailable or changed; no process signaled')
    # Never stop self, caller ancestry, or an alleged group shared with this manager.
    own_ancestors = set()
    current = os.getpid()
    while current in rows and current not in own_ancestors:
        own_ancestors.add(current)
        current = rows[current]['ppid']
    if pid in own_ancestors or pid <= 1:
        raise RuntimeError('root is a protected manager/ancestor process')
    if owned_group and not (pid == root['pgid'] == root['sid'] and
                            root['pgid'] != os.getpgrp() and root['sid'] != os.getsid(0)):
        raise RuntimeError('owned group requires a private start_new_session launch')
    ids = descendants(rows, pid)
    if owned_group:
        ids |= {p for p, row in rows.items() if row['sid'] == root['sid']}
    observed = [rows[p] for p in sorted(ids) if p in rows and live(rows[p])]
    return dict(version=1, root=root, owned_group=bool(owned_group), observed=observed,
                captured_at=time.time(), comprehensive_proof=False, limitation=LIMITATION)


def stop(snapshot, term_timeout=1.0, kill_timeout=1.0):
    """Signal only freshly matched observed identities, retain escapes across reparenting.

    scope_stopped is about observed processes only; comprehensive_proof is always
    False, including when an explicitly owned session becomes empty.
    """
    if not 0 <= term_timeout <= 10 or not 0 <= kill_timeout <= 10:
        raise ValueError('each stop timeout must be between 0 and 10 seconds')
    root = snapshot['root']
    if snapshot.get('version') != 1:
        raise RuntimeError('unsupported process snapshot')
    owned = snapshot.get('owned_group', False)
    if root['pid'] <= 1 or root['pid'] == os.getpid():
        raise RuntimeError('protected process')
    if owned and not (root['pid'] == root['pgid'] == root['sid'] and
                      root['sid'] != os.getsid(0) and root['pgid'] != os.getpgrp()):
        raise RuntimeError('snapshot does not identify a private session')
    observed = {key(row): dict(row) for row in snapshot['observed']}
    observed[key(root)] = dict(root)
    errors, mismatches, escaped, signals = [], {}, {}, []
    remaining = list(observed.values())
    root_anchor_lost = False

    def scan():
        nonlocal root_anchor_lost
        rows = inventory()
        root_now = rows.get(root['pid'])
        # A reused leader identity invalidates all future group discovery.
        if root_now and root_now['start'] != root['start']:
            root_anchor_lost = True
        matched = {r['pid'] for k, r in observed.items()
                   if live(rows.get(r['pid'])) and key(rows[r['pid']]) == k}
        discovered = set()
        for pid in matched:
            discovered |= descendants(rows, pid)
        if owned and not root_anchor_lost:
            discovered |= {p for p, r in rows.items() if r['sid'] == root['sid']}
        for pid in discovered:
            if pid in rows and live(rows[pid]):
                observed[key(rows[pid])] = rows[pid]
        active = []
        for k, old in observed.items():
            current = rows.get(old['pid'])
            if current and key(current) != k:
                mismatches[k] = old
                continue
            if live(current):
                if current['sid'] != root['sid']:
                    escaped[k] = current
                active.append(current)
        return active

    try:
        for signum, duration in ((signal.SIGTERM, term_timeout), (signal.SIGKILL, kill_timeout)):
            deadline = time.monotonic() + duration
            sent = set()
            while True:
                remaining = scan()
                if not remaining:
                    break
                # Descendants first; no killpg that could include an unvalidated PID.
                for row in sorted(remaining, key=lambda r: r['pid'] == root['pid']):
                    k = key(row)
                    if k in sent:
                        continue
                    current_rows = inventory()
                    fresh = current_rows.get(row['pid'])
                    if not live(fresh) or key(fresh) != k:
                        continue
                    # A forged/stale snapshot must never target manager ancestry.
                    ancestor = os.getpid()
                    protected = set()
                    while ancestor in current_rows and ancestor not in protected:
                        protected.add(ancestor)
                        ancestor = current_rows[ancestor]['ppid']
                    if row['pid'] in protected or row['pid'] <= 1:
                        errors.append('protected PID refused: ' + str(row['pid']))
                        sent.add(k)
                        continue
                    try:
                        os.kill(row['pid'], signum)
                        signals.append(dict(pid=row['pid'], start=row['start'], signal=int(signum)))
                    except ProcessLookupError:
                        pass
                    except OSError as error:
                        errors.append('signal failed for PID %s: %s' % (row['pid'], type(error).__name__))
                    sent.add(k)
                if time.monotonic() >= deadline:
                    break
                time.sleep(min(.05, max(0, deadline - time.monotonic())))
        remaining = scan()
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as error:
        errors.append('stop observation incomplete: ' + type(error).__name__)
    return dict(scope_stopped=not remaining and not errors and not mismatches,
                comprehensive_proof=False, requires_confirmation=True,
                root=root, owned_group=owned, observed=list(observed.values()),
                remaining=remaining, escaped=list(escaped.values()),
                identity_mismatches=list(mismatches.values()), errors=errors,
                signals=signals, captured_at=snapshot['captured_at'], completed_at=time.time(),
                limitation=LIMITATION)
