"""Task-attempt app-server runtime (the default for new sol workers). No shared Codex daemon or global config.

Only the supervisor owns the RPC connection and delivery journal. IPC takes task
id/generation, never arbitrary commands or endpoints. Ambiguous sends are held for
history reconciliation or an explicit retry, never automatically resent.
"""
import argparse
import datetime
import json
import os
import queue
import re
import signal
import sqlite3
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

import psutil

from . import store
from .app_transport import RPC, RPCError
from .processes import cleanup_processes, task_processes
from .runtime import CODEX_MEMORIES_OFF

BACKEND = 'app-server'
TERMINAL = ('completed', 'failed', 'interrupted')
BACKGROUND_HEARTBEAT_TIMEOUT = 60  # Longer than normal bounded RPC/history batches.


def enabled(task):
    return dict(task).get('backend') == BACKEND


def identity(pid):
    proc = psutil.Process(int(pid))
    return json.dumps({'pid': proc.pid, 'birth': proc.create_time(), 'pgid': os.getpgid(proc.pid)})


def process(raw):
    """None means confirmed gone/reused; unreadable identity raises instead of guessing."""
    if not raw:
        return None
    data = json.loads(raw)
    try:
        proc = psutil.Process(data['pid'])
        if proc.create_time() != data['birth'] or proc.status() == psutil.STATUS_ZOMBIE:
            return None
        if os.getpgid(proc.pid) != data['pgid']:
            raise RuntimeError('process group changed; stop refused')
        return proc
    except (psutil.NoSuchProcess, ProcessLookupError):
        return None


def stop_identity(raw, grace=2):
    proc = process(raw)
    if proc is None:
        return
    data = json.loads(raw)
    # Every owned root starts a new session. Never signal a caller's/shared group.
    if data['pgid'] != proc.pid or proc.pid == os.getpid():
        raise RuntimeError('not an owned process-group leader')
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    end = time.monotonic() + grace
    while process(raw) is not None and time.monotonic() < end:
        time.sleep(.05)
    if process(raw) is not None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        end = time.monotonic() + grace
        while process(raw) is not None and time.monotonic() < end:
            time.sleep(.05)
    if process(raw) is not None:
        raise RuntimeError(f'process {proc.pid} not confirmed stopped')


def validate(task):
    if not enabled(task) or not re.fullmatch(r'[0-9a-f]{32}', task['generation'] or ''):
        raise ValueError('not an app-server attempt')
    parent = Path(task['endpoint']).parent
    stat = parent.lstat()
    if parent.is_symlink() or stat.st_uid != os.getuid() or stat.st_mode & 0o777 != 0o700:
        raise ValueError('app-server directory is not private')
    if Path(task['endpoint']).name != 'app.sock' or task['control_endpoint'] != str(parent / 'control.sock'):
        raise ValueError('invalid stored app-server endpoints')


def control(task, command, timeout=25, **fields):
    validate(task)
    with socket.socket(socket.AF_UNIX) as peer:
        peer.settimeout(timeout)
        peer.connect(task['control_endpoint'])
        peer.sendall((json.dumps(dict(command=command, generation=task['generation'], **fields))+'\n').encode())
        with peer.makefile('r') as stream:
            raw = stream.readline(1024 * 1024)
        if not raw:
            raise ConnectionError('supervisor disconnected; delivery may be uncertain')
        result = json.loads(raw)
        if result.get('exception'):
            raise RuntimeError(result['exception'])
        return result


def health_detail(task):
    """(worker_alive, supervisor_error). The error is why a live supervisor is degraded; None when it is not."""
    try:
        sup, server = process(task['supervisor_identity']), process(task['server_identity'])
        if sup is None and server is None:
            return 'dead', None
        if sup is None or server is None:
            return 'unknown', None
        reply = control(task, 'health', timeout=3)
        return reply['worker_alive'], reply.get('supervisor_error')
    except Exception as exc:
        return 'unknown', f'control: {type(exc).__name__}: {exc}'


def health(task):
    return health_detail(task)[0]


def stop(con, task):
    # Cleanup uses recorded process identities, never the vanished/untrusted IPC path.
    roots=[p for p in (process(task['supervisor_identity']), process(task['server_identity'])) if p]
    captured = task_processes(task['worktree'], roots)
    viewers = con.execute('SELECT identity FROM worker_viewers WHERE task_id=? AND generation=?',
                          (task['id'], task['generation'])).fetchall()
    # Supervisor shutdown stops its server; independently verify stored server even if it crashed.
    stop_identity(task['supervisor_identity'])
    for row in viewers:
        stop_identity(row['identity'])
    stop_identity(task['server_identity'])
    receipt = cleanup_processes(task['worktree'], captured)
    for raw in [task['supervisor_identity'], task['server_identity'], *[r['identity'] for r in viewers]]:
        if process(raw) is not None:
            raise RuntimeError('app-server attempt not confirmed stopped')
    return receipt


def start(con, rt, task, prompt, model, pending=(), brief=None):
    from .runtime import worker_env
    generation = uuid.uuid4().hex
    directory = Path(tempfile.mkdtemp(prefix='oa-', dir='/tmp'))
    directory.chmod(0o700)
    database = con.execute('PRAGMA database_list').fetchone()[2]
    spec = dict(prompt=prompt, model=model, brief=brief, pending=[r['id'] for r in pending])
    (directory/'start.json').write_text(json.dumps(spec))
    (directory/'start.json').chmod(0o600)
    store.update_task(con, task['id'], backend=BACKEND, generation=generation,
                      endpoint=str(directory/'app.sock'), control_endpoint=str(directory/'control.sock'),
                      supervisor_identity=None, server_identity=None, active_turn=None,
                      turn_state='starting', last_completed_turn=None, session_id=None, socket=None,
                      model=model, job_id=None)
    env = worker_env(task['worktree'])
    # Use the package that launched this attempt, including an isolated checkout install. PYTHONPATH is
    # honored with -P; only the implicit cwd entry is dropped.
    env['PYTHONPATH'] = str(Path(__file__).resolve().parents[1]) + os.pathsep + env.get('PYTHONPATH','')
    with open(directory/'supervisor.log', 'a') as out:
        # -P: never put the worktree (cwd) on sys.path; it may hold its own, different orchd/.
        child = subprocess.Popen([sys.executable, '-P', '-m', 'orchd.app_worker', 'supervise', database,
                                  task['id'], generation], cwd=task['worktree'], env=env,
                                 stdin=subprocess.DEVNULL, stdout=out, stderr=out, start_new_session=True)
    store.update_task(con, task['id'], supervisor_identity=identity(child.pid), job_id=str(child.pid))
    deadline = time.monotonic()+35
    try:
        while time.monotonic() < deadline:
            current = store.get_task(con, task['id'])
            if current['session_id'] and current['turn_state'] != 'starting' and Path(current['control_endpoint']).exists():
                if current['turn_state'] == 'unknown':
                    raise RuntimeError('initial turn delivery uncertain; inspect journal')
                return current
            if child.poll() is not None:
                break
            time.sleep(.05)
        raise RuntimeError('app-server startup failed: '+(directory/'supervisor.log').read_text()[-1200:])
    except Exception:
        # The supervisor publishes server identity before any RPC/start. Preserve rows/pending on failure.
        stop(con, store.get_task(con, task['id']))
        raise


def app_server_command(task):
    return [task['codex_bin'],*CODEX_MEMORIES_OFF,'app-server','--listen','unix://'+task['endpoint']]


def preload():
    """Import everything the supervisor loads lazily, so the worker editing orchd on disk cannot skew it later."""
    # config, models and paths are reached through Runtime() and core.auto_retry_pending; the isolation test walks
    # those paths and fails if anything else is still imported late.
    from . import config, core, models, paths, quota, routing  # noqa: F401
    from .runtime import Runtime  # noqa: F401


# An interrupt is urgent: wait for the delivery lane this long, then fail loudly (never queue).
# Kept under control()'s 25 s socket timeout together with the 15 s confirmation window.
INTERRUPT_LOCK_WAIT = 10
# Automatic delivery pause after a rate-limit refusal without a usable reset time.
LIMIT_BACKOFF_BASE = 30
LIMIT_BACKOFF_MAX = 300


class Supervisor:
    def __init__(self, database, task_id, generation):
        self.maintenance_error = None
        self.database = database
        self.connections = threading.local()
        self.connections.con = store.connect(database)
        self.work_lock = threading.Lock()
        self.history_scan = None
        self.rpc_health_error = None
        self.rpc_state = None
        self.health_refresh_at = 0
        self.task_id, self.generation = task_id, generation
        self.rpc = None
        self.server = None
        self.server_identity = None
        self.stopping = False
        self.interrupting = False
        self.reconnect_at = 0
        self.deferred_flush = None
        self.deferred_limit = None
        self.reconcile_pending = False
        self.housekeeping_at = 0
        self.background_heartbeat = None
        self.background_error = None
        self.backoff = 0
        self.limit_until = 0  # monotonic; automatic delivery is paused until then after a rate-limit refusal
        self.limit_wall = None
        self.limit_failures = 0
        self.rejected = None  # pending ids the server explicitly refused; in memory, so a restart retries once

    @property
    def con(self):
        # Each thread owns its SQLite connection. Schema setup happens once in __init__.
        if not hasattr(self.connections, 'con'):
            con = sqlite3.connect(self.database, timeout=2, isolation_level=None)
            con.row_factory = sqlite3.Row
            self.connections.con = con
        return self.connections.con

    @con.setter
    def con(self, value):
        if not hasattr(self, 'connections'):
            self.connections = threading.local()
        self.connections.con = value

    def task(self):
        return store.get_task(self.con, self.task_id)

    def current(self):
        t = self.task()
        return enabled(t) and t['generation'] == self.generation and t['status'] != 'closed'

    def update(self, **fields):
        fields['updated_at'] = time.time()
        changed = self.con.execute('UPDATE tasks SET '+','.join(f'{key}=?' for key in fields)+
            " WHERE id=? AND generation=? AND backend='app-server' AND status<>'closed'",
            (*fields.values(),self.task_id,self.generation)).rowcount
        if changed != 1:
            raise RuntimeError('superseded attempt')

    def event(self, msg):
        if not self.current():
            return
        params = msg.get('params', {})
        if msg.get('method') == 'account/rateLimits/updated':
            from .quota import Cache
            try:
                Cache().codex(params, source='codex.updated')
            except Exception:
                pass  # Optional telemetry must not break delivery.
            return
        task = self.task()
        if params.get('threadId') != task['session_id']:
            return
        method = msg.get('method')
        if method in ('turn/started', 'turn/completed'):
            self.collect_quota(force=True)
        # Task-scoped evidence for native viewer input and completion, never an Orch inbox notification.
        if method in ('turn/started', 'turn/completed', 'item/started', 'item/completed'):
            key = 'item' if method.startswith('item/') else 'turn'
            value = params.get(key, {})
            metadata = {k: value[k] for k in ('id', 'type', 'status') if k in value}
            evidence = {'method': method, 'params': {
                k: params[k] for k in ('threadId', 'turnId') if k in params}}
            evidence['params'][key] = metadata
            store.add_message(self.con, self.task_id, 'app_event', json.dumps(evidence))
        if method == 'turn/started':
            turn = params['turn']
            self.update(active_turn=turn['id'], turn_state='active')
        elif method == 'turn/completed':
            turn = params['turn']
            if task['active_turn'] != turn['id']:
                return  # duplicate/old completion cannot claim the newer turn's queue
            self.update(active_turn=None, turn_state=turn['status'], last_completed_turn=turn['id'])
            if turn['status'] != 'completed':
                self.update(note=f"app-server turn {turn['status']}: {turn.get('error')}")
            if turn['status'] == 'failed':
                from .core import rate_limit_notice
                from .runtime import Runtime
                if rate_limit_notice(self.con, Runtime(), self.task_id, turn.get('error'),
                                     self.generation + ':' + turn['id'], generation=self.generation):
                    self.deferred_flush = None
                    return  # hold queued inputs for the replacement worker
            if not self.interrupting:
                self.deferred_flush = (self.generation, task['session_id'], turn['id'])

    def drain(self):
        while True:
            try:
                self.event(self.rpc.events.get_nowait())
            except queue.Empty:
                break

    def collect_quota(self, force=False):
        from .quota import Cache, refresh
        try:
            refresh(Cache(), rpc=self.rpc, force=force)
        except Exception:
            pass  # Quota telemetry must not break delivery or shutdown.

    def handle_deferred_limit(self):
        # send() may run under the delivery lock. Notify/start helper only after
        # releasing it, since notification and retry both acquire that lock.
        if self.deferred_limit is not None:
            error, attempt = self.deferred_limit
            self.deferred_limit = None
            from .core import rate_limit_notice
            from .runtime import Runtime
            rate_limit_notice(self.con, Runtime(), self.task_id, error, attempt, generation=self.generation)

    def viewers(self):
        live = []
        for row in self.con.execute('SELECT identity FROM worker_viewers WHERE task_id=? AND generation=?',
                                    (self.task_id, self.generation)).fetchall():
            if process(row['identity']) is not None:
                live.append(row['identity'])
            else:
                self.con.execute('DELETE FROM worker_viewers WHERE task_id=? AND generation=? AND identity=?',
                                 (self.task_id, self.generation, row['identity']))
        return live

    def history(self, thread_id):
        """Bound each batch to four pages/five seconds; keep the cursor on timeout.

        Partial history must not update turn state or release uncertain deliveries.
        The next maintenance pass continues at the first unconsumed page.
        """
        if self.history_scan is None or self.history_scan['thread'] != thread_id:
            self.history_scan = dict(thread=thread_id, turns=[], cursor=None)
        scan = self.history_scan
        deadline = time.monotonic() + 5
        for _ in range(4):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            page = self.rpc.call('thread/turns/list',
                                 {'threadId':thread_id, 'cursor':scan['cursor'], 'limit':100,
                                  'sortDirection':'asc', 'itemsView':'full'}, timeout=remaining)
            scan['turns'].extend(page['data'])
            scan['cursor'] = page.get('nextCursor')
            if not scan['cursor']:
                self.history_scan = None
                return scan['turns']
        self.reconcile_pending = True
        return None

    def reconcile(self):
        """Resume subscription and read authoritative history after a connection gap.

        No uncertain delivery is resent, even when history does not yet show it. A
        positively matching client id is sufficient to finish its durable receipt.
        """
        task = self.task()
        if self.history_scan is None:
            self.rpc.call('thread/resume', {'threadId': task['session_id']}, timeout=5)
        turns = self.history(task['session_id'])
        if turns is None:
            return False
        for delivery in self.con.execute("SELECT * FROM worker_deliveries WHERE task_id=? AND generation=? "
                                         "AND state IN ('sending','uncertain')", (self.task_id,self.generation)).fetchall():
            for turn in turns:
                if any(item.get('clientId') == delivery['id'] for item in turn.get('items', [])):
                    self.receipt(delivery['id'], turn['id'], json.loads(delivery['message_ids']))
                    break
        active = next((t for t in reversed(turns) if t['status'] == 'inProgress'), None)
        if active:
            self.update(active_turn=active['id'], turn_state='active')
        elif turns:
            last = turns[-1]
            self.update(active_turn=None, turn_state=last['status'], last_completed_turn=last['id'])
            if last['status'] == 'failed':
                from .core import rate_limit_notice
                from .runtime import Runtime
                if rate_limit_notice(self.con, Runtime(), self.task_id, last.get('error'),
                                     self.generation + ':' + last['id'], generation=self.generation):
                    self.deferred_flush = None
                    return
            self.deferred_flush=(self.generation,task['session_id'],last['id'])
        else:
            self.update(active_turn=None, turn_state='idle')
        return True

    def receipt(self, delivery_id, turn_id, ids):
        with store.immediate(self.con):
            if not self.current():
                raise RuntimeError('superseded delivery')
            for mid in ids:
                row = self.con.execute('SELECT * FROM messages WHERE id=? AND task_id=? AND read_at IS NULL',
                                       (mid,self.task_id)).fetchone()
                if row:
                    store.mark_read(self.con, [mid])
                    store.add_message(self.con,self.task_id,'answer',row['body'], evidence=row['evidence'],
                                      entry_reply_id=row['entry_reply_id'])
            self.con.execute("UPDATE worker_deliveries SET state='delivered',turn_id=?,error=NULL WHERE id=?",
                             (turn_id,delivery_id))
            self.update(active_turn=turn_id, turn_state='active')

    def reconcile_if_needed(self):
        if not self.reconcile_pending or time.monotonic() < self.reconnect_at:
            return
        self.reconnect_at = time.monotonic() + 2
        if self.reconcile() is False:
            return
        self.reconcile_pending = bool(self.con.execute(
            "SELECT 1 FROM worker_deliveries WHERE task_id=? AND generation=? "
            "AND state IN ('sending','uncertain') LIMIT 1",
            (self.task_id, self.generation)).fetchone())

    def send(self, text, ids):
        task = self.task()
        delivery_id = uuid.uuid4().hex
        self.con.execute('INSERT INTO worker_deliveries VALUES(?,?,?,?,?,?,?,?,?)',
                         (delivery_id,self.task_id,self.generation,task['session_id'],None,json.dumps(ids),
                          'sending',None,time.time()))  # journal before bytes leave
        try:
            result = self.rpc.call('turn/start', {'threadId': task['session_id'],
                       'input': [{'type':'text','text':text,'text_elements':[]}],
                       'clientUserMessageId': delivery_id, 'model': task['model'],
                       'approvalPolicy':'never', 'sandboxPolicy': {'type':'dangerFullAccess'}})
            self.receipt(delivery_id, result['turn']['id'], ids)
            self.rejected = None
            self.limit_until = 0
            self.limit_failures = 0
            return dict(status='delivered',delivered=len(ids),pending=store.pending_answer_count(self.con,self.task_id))
        except RPCError as exc:  # definitive server rejection: bytes did not start a turn
            self.con.execute("UPDATE worker_deliveries SET state='rejected',error=? WHERE id=?",(str(exc),delivery_id))
            from .routing import limited
            if limited(exc.error):
                self.deferred_limit = (exc.error, self.generation + ':' + delivery_id)
                self.pause_for_limit(exc.error)
            else:
                # Not transient: retrying only piles up journal rows. Hold automatic delivery until a new
                # answer, answer(flush=true) or retry; the text stays pending.
                self.rejected = frozenset(ids)
                self.maintenance_error = (f'turn/start rejected: {exc}; automatic delivery paused, text retained. '
                                          'Fix the cause, then answer(flush=true) or retry')
                self.set_owned_note(f'app-server supervisor: {self.maintenance_error}')
            return dict(status='failed',delivered=0,pending=store.pending_answer_count(self.con,self.task_id),error=str(exc))
        except Exception as exc:
            self.con.execute("UPDATE worker_deliveries SET state='uncertain',error=? WHERE id=?",(str(exc),delivery_id))
            self.update(turn_state='unknown',note='uncertain app-server delivery; pending held, inspect worker_deliveries')
            self.reconcile_pending = True
            return dict(status='failed',uncertain=True,delivered=0,
                        pending=store.pending_answer_count(self.con,self.task_id),error=str(exc))

    def pause_for_limit(self, error):
        """Hold automatic delivery after a rate-limit refusal: until the reset time the error carries, else a
        capped exponential backoff. The text stays pending; nothing is journaled while paused."""
        self.limit_failures += 1
        delay = min(LIMIT_BACKOFF_MAX, LIMIT_BACKOFF_BASE * 2 ** (self.limit_failures - 1))
        reset = error.get('resetsAt') or error.get('resets_at') if isinstance(error, dict) else None
        if isinstance(reset, (int, float)) and not isinstance(reset, bool) and reset > time.time():
            delay = reset - time.time()
        self.limit_until = time.monotonic() + delay
        self.limit_wall = time.time() + delay
        when = datetime.datetime.fromtimestamp(self.limit_wall, datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        self.maintenance_error = (f'rate limit: turn/start refused; automatic delivery paused, text retained; '
                                  f'next attempt {when} (answer flush=true tries once now)')
        self.set_owned_note(f'app-server supervisor: {self.maintenance_error}')

    def flush(self, guard=None, ignore_viewer=False):
        with store.task_delivery(self.con,[self.task_id],timeout=.1):
            task = self.task()
            pending = store.pending_answers(self.con,self.task_id)
            base = dict(delivered=0,pending=len(pending))
            if not self.current() or guard and guard != (task['generation'],task['session_id'],task['last_completed_turn']):
                return dict(status='skipped',reason='superseded',**base)
            from .core import auto_retry_pending
            if auto_retry_pending(self.con, task):
                return dict(status='queued',reason='automatic provider retry pending',**base)
            uncertain = self.con.execute("SELECT 1 FROM worker_deliveries WHERE task_id=? AND generation=? "
                                         "AND state IN ('sending','uncertain') LIMIT 1",(self.task_id,self.generation)).fetchone()
            if uncertain:
                return dict(status='failed',uncertain=True,error='delivery uncertain; retry/reconcile first',**base)
            if task['active_turn'] or (not ignore_viewer and self.viewers()) or task['turn_state'] in ('starting','unknown'):
                return dict(status='queued',**base)
            if not pending:
                return dict(status='delivered',**base)
            return self.send('\n\n'.join(f"[orchd answer {self.task_id}]\n{r['body']}" for r in pending),
                             [r['id'] for r in pending])

    def interrupt(self, expected_turn=None, guarded=False):
        task = self.task()
        turn = task['active_turn']
        if guarded and turn and turn != expected_turn:
            # Completion/flush or another client already owns a newer turn. The correction
            # may have reached that turn; never cancel it using an older caller's snapshot.
            pending=store.pending_answer_count(self.con,self.task_id)
            return dict(status='queued' if pending else 'delivered',interrupted=False,
                        delivered=0,pending=pending,reason='turn changed; newer turn left running')
        if not turn:
            return dict(self.flush(ignore_viewer=True),interrupted=False)
        self.interrupting = True
        try:
            self.rpc.call('turn/interrupt', {'threadId':task['session_id'],'turnId':turn})
            deadline = time.monotonic()+15
            while time.monotonic()<deadline:
                self.drain()  # no delivery lock held; callback only records cancellation
                current = self.task()
                if current['last_completed_turn'] == turn:
                    if current['turn_state'] != 'interrupted':
                        return dict(status='failed',interrupted=False,error='turn did not finish interrupted')
                    # Codex confirms cancellation of its tool calls before this event.
                    try:
                        return dict(self.flush(ignore_viewer=True),interrupted=True)
                    except TimeoutError:
                        self.deferred_flush = (self.generation, task['session_id'], turn)
                        return dict(status='queued', interrupted=True, delivered=0,
                            pending=store.pending_answer_count(self.con, self.task_id))
                time.sleep(.05)
            return dict(status='failed',interrupted=False,error='interrupt not confirmed; correction stays queued')
        finally:
            self.interrupting=False

    def command(self, request):
        if request.get('generation') != self.generation or not self.current():
            raise ValueError('superseded attempt')
        cmd = request['command']
        if cmd == 'health':
            # Never call RPC or wait for the delivery lock on this read-only path.
            task = self.task()
            error = self.maintenance_error or self.rpc_health_error
            if self.rpc.error:
                error = error or f'{type(self.rpc.error).__name__}: {self.rpc.error}'
            background_error = self.background_error
            if self.background_heartbeat is not None and \
                    time.monotonic() - self.background_heartbeat > BACKGROUND_HEARTBEAT_TIMEOUT:
                background_error = background_error or 'supervisor background heartbeat expired'
            error = background_error or error
            state = self.rpc_state or task['turn_state']
            return dict(worker_alive='unknown' if background_error or self.rpc.error or self.rpc_health_error else
                        'active' if state == 'active' else 'idle' if state in
                        ('idle', 'completed', 'failed', 'interrupted') else 'unknown',
                        supervisor_error=error, supervisor_busy=self.work_lock.locked())
        if cmd == 'flush':
            self.drain()
            if not request.get('guard'):
                self.rejected = None  # an explicit flush always sends once and reports the result
                self.limit_until = 0
            return self.flush(tuple(request['guard']) if request.get('guard') else None)
        if cmd == 'interrupt':
            task=self.task()
            if 'expected_turn' not in request or request.get('expected_generation')!=self.generation or \
                    request.get('expected_thread')!=task['session_id']:
                raise ValueError('interrupt requires matching generation/thread/turn snapshot')
            self.drain()
            return self.interrupt(request['expected_turn'],guarded=True)
        if cmd == 'viewer':
            # Peer may register only its own wrapper identity; private IPC is trusted to this OS user.
            raw = request['identity']
            proc = process(raw)
            if proc is None or os.getpgid(proc.pid)!=proc.pid:
                raise ValueError('viewer must be a live private process group')
            args=proc.cmdline()
            if 'orchd.app_worker' not in args or 'viewer' not in args or self.task_id not in args or self.generation not in args:
                raise ValueError('viewer identity does not match this attempt')
            self.con.execute('INSERT OR IGNORE INTO worker_viewers VALUES(?,?,?)',(self.task_id,self.generation,raw))
            return dict(status='registered')
        raise ValueError('unknown control command')

    def maintain(self):
        """Periodic work. Any failure (TimeoutError included) is recorded and backed off, never raised."""
        try:
            self.drain()
            if time.monotonic() >= self.health_refresh_at:
                self.health_refresh_at = time.monotonic() + 2
                try:
                    thread = self.rpc.call('thread/read', {'threadId':self.task()['session_id']}, timeout=2)['thread']
                    self.rpc_state = thread.get('status', {}).get('type')
                    self.rpc_health_error = None
                except Exception as exc:
                    self.rpc_health_error = f'{type(exc).__name__}: {exc}'
                    raise
            self.collect_quota()  # Single-flight TTL also refreshes idle workers, without inference.
            self.handle_deferred_limit()
            self.reconcile_if_needed()
            if self.deferred_flush:
                if not self.auto_paused():
                    self.flush(guard=self.deferred_flush)
                self.deferred_flush = None
            # Pending text is durable: delivery must not depend on another client flush
            # or on a completion event observed by this particular supervisor.
            if not self.reconcile_pending and store.pending_answer_count(self.con, self.task_id) \
                    and not self.auto_paused():
                self.flush()
            self.handle_deferred_limit()
            # Viewer death releases human ownership; an idle FIFO can then flush.
            had=self.con.execute('SELECT 1 FROM worker_viewers WHERE task_id=? AND generation=?',
                                 (self.task_id,self.generation)).fetchone()
            if had and not self.viewers():
                self.deferred_flush = (self.generation,self.task()['session_id'],
                                       self.task()['last_completed_turn'])
            if not self.reconcile_pending and not self.deferred_flush and self.rpc_health_error is None \
                    and self.rejected is None and time.monotonic() >= self.limit_until:
                self.maintenance_error = None
                self.backoff = 0
                self.clear_note()
        except Exception as exc:
            self.fail_housekeeping(exc)

    def auto_paused(self):
        """True while a rate-limit pause runs, or while the pending answers are exactly the ones the server
        refused (any change to those resumes)."""
        if time.monotonic() < self.limit_until:
            return True
        if self.rejected is not None and frozenset(r['id'] for r in store.pending_answers(
                self.con, self.task_id)) != self.rejected:
            self.rejected = None
        return self.rejected is not None

    def fail_housekeeping(self, exc):
        """Record why background work failed and delay the next attempt (0.2s doubling to 5s)."""
        error = f'{type(exc).__name__}: {exc}'
        self.maintenance_error = error
        self.backoff = min(5, self.backoff * 2 or .2)
        self.housekeeping_at = time.monotonic() + self.backoff
        self.set_owned_note(f'app-server supervisor: {error}')

    def set_owned_note(self, note):
        try:
            # Persist exact ownership in the same transaction as the note. A replacement
            # supervisor can recover it; no prefix matching can claim a foreign note.
            with store.immediate(self.con):
                owned = self.con.execute(
                    'SELECT note FROM worker_supervisor_notes WHERE task_id=? AND generation=?',
                    (self.task_id, self.generation)).fetchone()
                changed = self.con.execute(
                    "UPDATE tasks SET note=? WHERE id=? AND generation=? AND backend='app-server' "
                    "AND status<>'closed' AND (note IS NULL OR note='' OR note=?) AND note IS NOT ?",
                    (note, self.task_id, self.generation, owned['note'] if owned else None, note)).rowcount
                if changed:
                    self.con.execute('INSERT OR REPLACE INTO worker_supervisor_notes VALUES(?,?,?)',
                                     (self.task_id, self.generation, note))
        except Exception:
            pass  # the note is best effort; health still reports maintenance_error

    def clear_note(self):
        """Recovery removes only the durable exact note owned by this task attempt."""
        try:
            with store.immediate(self.con):
                self.con.execute("UPDATE tasks SET note=NULL WHERE id=? AND generation=? AND status<>'closed' "
                                 "AND note=(SELECT note FROM worker_supervisor_notes WHERE task_id=? AND generation=?)",
                                 (self.task_id, self.generation, self.task_id, self.generation))
                self.con.execute('DELETE FROM worker_supervisor_notes WHERE task_id=? AND generation=?',
                                 (self.task_id, self.generation))
        except Exception:
            pass

    def housekeep(self, task):
        """Server liveness, reconnect, reconcile and maintenance. False means the server is gone."""
        if self.server.poll() is not None:
            self.update(turn_state='dead')
            self.fail_housekeeping(RuntimeError('task app-server exited; pending retained'))
            return False
        if self.rpc.error:
            self.update(turn_state='unknown')
            if self.maintenance_error is None:
                self.fail_housekeeping(self.rpc.error)
            if time.monotonic()<self.reconnect_at:
                return True
            self.reconnect_at=time.monotonic()+2
            self.rpc.close();self.rpc=RPC(task['endpoint'])
            self.history_scan = None
            self.health_refresh_at = 0
            self.reconcile_pending = True
        self.maintain()
        return True

    def serve(self, listener):
        try:
            peer,_=listener.accept()
        except socket.timeout:
            return
        # A control RPC can itself be slow. Its peer thread must not hold accept up.
        threading.Thread(target=self.serve_peer, args=(peer,), daemon=True).start()

    def serve_peer(self, peer):
        try:
            with peer:
                peer.settimeout(2)
                with peer.makefile('r') as stream:
                    request=json.loads(stream.readline(1024*1024))
                acquired = False
                try:
                    if request.get('command') == 'health':
                        reply = self.command(request)
                    else:
                        interrupt = request.get('command') == 'interrupt'
                        # Urgent: wait for the lane (bounded) instead of reporting a busy that nobody retries.
                        acquired = self.work_lock.acquire(timeout=INTERRUPT_LOCK_WAIT) if interrupt \
                            else self.work_lock.acquire(blocking=False)
                        if acquired:
                            reply = self.command(request)
                        elif request.get('generation') != self.generation or not self.current():
                            raise ValueError('superseded attempt')
                        elif interrupt:
                            reply = dict(status='failed', interrupted=False, delivered=0, not_sent=True,
                                         pending=store.pending_answer_count(self.con, self.task_id),
                                         error='interrupt not delivered: supervisor busy; retry', supervisor_busy=True)
                        elif request.get('command') != 'flush':
                            raise RuntimeError('supervisor busy; retry control later')
                        else:
                            if request.get('command') == 'flush' and not request.get('guard'):
                                self.rejected = None  # explicit flush lifts the pause; the next tick sends once
                                self.limit_until = 0
                            reply = dict(status='queued', interrupted=False, delivered=0,
                                         pending=store.pending_answer_count(self.con, self.task_id),
                                         reason='supervisor busy; text retained; will be delivered automatically',
                                         supervisor_busy=True, supervisor_error=self.maintenance_error)
                except Exception as exc:
                    reply={'exception':f'{type(exc).__name__}: {exc}'}
                finally:
                    if acquired:
                        self.work_lock.release()
                peer.sendall((json.dumps(reply)+'\n').encode())
        except (OSError, ValueError):
            pass
        finally:
            if hasattr(self.connections, 'con'):
                self.connections.con.close()

    def background(self, task, stop):
        try:
            while not stop.is_set() and not self.stopping:
                self.background_heartbeat = time.monotonic()
                if time.monotonic() >= self.housekeeping_at and self.work_lock.acquire(blocking=False):
                    try:
                        if not self.current() or not self.housekeep(task):
                            self.stopping = True
                    except Exception as exc:
                        self.fail_housekeeping(exc)
                    finally:
                        self.work_lock.release()
                stop.wait(.2)
        except BaseException as exc:
            # Keep control alive so Operator can inspect the failure and explicitly retry.
            # Never restart this lane blindly: an interrupted send may be uncertain.
            self.background_error = f'supervisor background stopped: {type(exc).__name__}: {exc}'
        finally:
            if not stop.is_set() and not self.stopping:
                self.background_error = self.background_error or 'supervisor background stopped unexpectedly'
            if hasattr(self.connections, 'con'):
                self.connections.con.close()

    def loop(self, listener, task):
        """One serialized RPC/delivery lane; accept and health never wait for it."""
        stop = threading.Event()
        worker = threading.Thread(target=self.background, args=(task, stop), daemon=True)
        worker.start()
        try:
            while not self.stopping and self.current():
                self.serve(listener)
        finally:
            stop.set()
            worker.join(.3)
            # run() closes RPC to wake a pending call during process shutdown.

    def run(self):
        preload()
        task = self.task()
        validate(task)
        directory=Path(task['endpoint']).parent
        spec=json.loads((directory/'start.json').read_text())
        (directory/'start.json').unlink()
        signal.signal(signal.SIGTERM,lambda *_:setattr(self,'stopping',True))
        signal.signal(signal.SIGINT,lambda *_:setattr(self,'stopping',True))
        try:
            with open(directory/'server.log','a') as out:
                self.server=subprocess.Popen(app_server_command(task),
                    cwd=task['worktree'],stdout=out,stderr=out,stdin=subprocess.DEVNULL,start_new_session=True)
            self.server_identity=identity(self.server.pid)
            self.update(server_identity=self.server_identity)
            deadline=time.monotonic()+15
            while not Path(task['endpoint']).exists() and time.monotonic()<deadline and self.server.poll() is None:
                time.sleep(.05)
            os.chmod(task['endpoint'],0o600)
            self.rpc=RPC(task['endpoint'])
            from .quota import Cache, refresh
            try:
                refresh(Cache(), rpc=self.rpc)
            except Exception:
                pass
            thread=self.rpc.call('thread/start',{'cwd':task['worktree'],'model':spec['model'],
                              'approvalPolicy':'never','sandbox':'danger-full-access',
                              'developerInstructions':spec.get('brief')})['thread']
            self.update(session_id=thread['id'])
            with socket.socket(socket.AF_UNIX) as listener:
                listener.bind(task['control_endpoint']);os.chmod(task['control_endpoint'],0o600)
                listener.listen(8);listener.settimeout(.2)
                result=self.send(spec['prompt'],spec['pending'])
                if result['status']!='delivered':
                    raise RuntimeError(str(result))
                self.loop(listener, task)
        finally:
            if self.rpc:
                self.rpc.close()
            if self.server:
                stop_identity(self.server_identity)
                self.server.wait(timeout=3)
            self.con.close()


def viewer(database, task_id, generation):
    con=store.connect(database)
    task=store.get_task(con,task_id)
    if task['generation']!=generation or task['status']=='closed':
        raise ValueError('superseded/closed viewer')
    validate(task)
    # Ghostty's shell is not a group leader. A wrapper owns a new group for targeted cleanup.
    if os.getpgrp()!=os.getpid():
        os.setsid()
    control(task,'viewer',identity=identity(os.getpid()))
    con.close()
    from .runtime import worker_env
    # exec keeps the registered PID/birth/group across the native TUI's lifetime.
    # Mark its descendants even if closing the terminal reparents them.
    os.chdir(task['worktree'])
    os.execvpe(task['codex_bin'], [task['codex_bin'],'--remote','unix://'+task['endpoint'],
                                '--no-alt-screen','resume',task['session_id']], worker_env(task['worktree']))


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('mode',choices=['supervise','viewer'])
    parser.add_argument('database');parser.add_argument('task_id');parser.add_argument('generation')
    args=parser.parse_args()
    if args.mode=='supervise':
        Supervisor(args.database,args.task_id,args.generation).run()
    else:
        sys.exit(viewer(args.database,args.task_id,args.generation))


if __name__=='__main__':
    main()
