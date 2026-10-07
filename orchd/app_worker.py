"""Opt-in task-attempt app-server runtime. No shared Codex daemon or global config.

Only the supervisor owns the RPC connection and delivery journal. IPC takes task
id/generation, never arbitrary commands or endpoints. Ambiguous sends are held for
history reconciliation or an explicit retry, never automatically resent.
"""
import argparse
import json
import os
import queue
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import psutil

from . import store
from .app_transport import RPC, RPCError
from .processes import cleanup_processes, task_processes

BACKEND = 'app-server'
TERMINAL = ('completed', 'failed', 'interrupted')


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


def health(task):
    try:
        sup, server = process(task['supervisor_identity']), process(task['server_identity'])
        if sup is None and server is None:
            return 'dead'
        if sup is None or server is None:
            return 'unknown'
        return control(task, 'health', timeout=3)['worker_alive']
    except Exception:
        return 'unknown'


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
    # Use the package that launched this attempt, including an isolated checkout install.
    env['PYTHONPATH'] = str(Path(__file__).resolve().parents[1]) + os.pathsep + env.get('PYTHONPATH','')
    with open(directory/'supervisor.log', 'a') as out:
        child = subprocess.Popen([sys.executable, '-m', 'orchd.app_worker', 'supervise', database,
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


class Supervisor:
    def __init__(self, database, task_id, generation):
        self.con = store.connect(database)
        self.task_id, self.generation = task_id, generation
        self.rpc = None
        self.server = None
        self.server_identity = None
        self.stopping = False
        self.interrupting = False
        self.reconnect_at = 0
        self.deferred_flush = None
        self.reconcile_pending = False

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
        task = self.task()
        if params.get('threadId') != task['session_id']:
            return
        method = msg.get('method')
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
            if not self.interrupting:
                self.deferred_flush = (self.generation, task['session_id'], turn['id'])

    def drain(self):
        while True:
            try:
                self.event(self.rpc.events.get_nowait())
            except queue.Empty:
                break

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
        turns, cursor = [], None
        while True:
            page = self.rpc.call('thread/turns/list', {'threadId':thread_id, 'cursor':cursor, 'limit':100,
                                                      'sortDirection':'asc', 'itemsView':'full'})
            turns.extend(page['data'])
            cursor = page.get('nextCursor')
            if not cursor:
                return turns

    def reconcile(self):
        """Resume subscription and read authoritative history after a connection gap.

        No uncertain delivery is resent, even when history does not yet show it. A
        positively matching client id is sufficient to finish its durable receipt.
        """
        task = self.task()
        self.rpc.call('thread/resume', {'threadId': task['session_id']})
        turns = self.history(task['session_id'])
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
            self.deferred_flush=(self.generation,task['session_id'],last['id'])
        else:
            self.update(active_turn=None, turn_state='idle')

    def receipt(self, delivery_id, turn_id, ids):
        with store.immediate(self.con):
            if not self.current():
                raise RuntimeError('superseded delivery')
            for mid in ids:
                row = self.con.execute('SELECT * FROM messages WHERE id=? AND task_id=? AND read_at IS NULL',
                                       (mid,self.task_id)).fetchone()
                if row:
                    store.mark_read(self.con, [mid])
                    store.add_message(self.con,self.task_id,'answer',row['body'])
            self.con.execute("UPDATE worker_deliveries SET state='delivered',turn_id=?,error=NULL WHERE id=?",
                             (turn_id,delivery_id))
            self.update(active_turn=turn_id, turn_state='active')

    def reconcile_if_needed(self):
        if not self.reconcile_pending or time.monotonic() < self.reconnect_at:
            return
        self.reconnect_at = time.monotonic() + 2
        self.reconcile()
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
            return dict(status='delivered',delivered=len(ids),pending=store.pending_answer_count(self.con,self.task_id))
        except RPCError as exc:  # definitive server rejection: bytes did not start a turn
            self.con.execute("UPDATE worker_deliveries SET state='rejected',error=? WHERE id=?",(str(exc),delivery_id))
            self.update(note=f"app-server delivery rejected; pending retained: {exc}")
            return dict(status='failed',delivered=0,pending=store.pending_answer_count(self.con,self.task_id),error=str(exc))
        except Exception as exc:
            self.con.execute("UPDATE worker_deliveries SET state='uncertain',error=? WHERE id=?",(str(exc),delivery_id))
            self.update(turn_state='unknown',note='uncertain app-server delivery; pending held, inspect worker_deliveries')
            self.reconcile_pending = True
            return dict(status='failed',uncertain=True,delivered=0,
                        pending=store.pending_answer_count(self.con,self.task_id),error=str(exc))

    def flush(self, guard=None, ignore_viewer=False):
        with store.task_delivery(self.con,[self.task_id],timeout=.1):
            task = self.task()
            pending = store.pending_answers(self.con,self.task_id)
            base = dict(delivered=0,pending=len(pending))
            if not self.current() or guard and guard != (task['generation'],task['session_id'],task['last_completed_turn']):
                return dict(status='skipped',reason='superseded',**base)
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
            thread = self.rpc.call('thread/read', {'threadId':self.task()['session_id']})['thread']
            state = thread.get('status',{}).get('type')
            return dict(worker_alive='active' if state=='active' else 'idle' if state=='idle' else 'unknown')
        if cmd == 'flush':
            self.drain()
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

    def run(self):
        task = self.task()
        validate(task)
        directory=Path(task['endpoint']).parent
        spec=json.loads((directory/'start.json').read_text())
        (directory/'start.json').unlink()
        signal.signal(signal.SIGTERM,lambda *_:setattr(self,'stopping',True))
        signal.signal(signal.SIGINT,lambda *_:setattr(self,'stopping',True))
        try:
            with open(directory/'server.log','a') as out:
                self.server=subprocess.Popen([task['codex_bin'],'app-server','--listen','unix://'+task['endpoint']],
                    cwd=task['worktree'],stdout=out,stderr=out,stdin=subprocess.DEVNULL,start_new_session=True)
            self.server_identity=identity(self.server.pid)
            self.update(server_identity=self.server_identity)
            deadline=time.monotonic()+15
            while not Path(task['endpoint']).exists() and time.monotonic()<deadline and self.server.poll() is None:
                time.sleep(.05)
            os.chmod(task['endpoint'],0o600)
            self.rpc=RPC(task['endpoint'])
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
                while not self.stopping and self.current():
                    try:
                        if self.server.poll() is not None:
                            self.update(turn_state='dead',note='task app-server exited; pending retained')
                            break
                        if self.rpc.error:
                            self.update(turn_state='unknown',note='RPC disconnected; pending retained')
                            if time.monotonic()<self.reconnect_at:
                                time.sleep(.2);continue
                            self.reconnect_at=time.monotonic()+2
                            self.rpc.close();self.rpc=RPC(task['endpoint'])
                            self.reconcile()
                        self.drain()
                        self.reconcile_if_needed()
                        if self.deferred_flush:
                            self.flush(guard=self.deferred_flush)
                            self.deferred_flush = None
                        # Viewer death releases human ownership; an idle FIFO can then flush.
                        had=self.con.execute('SELECT 1 FROM worker_viewers WHERE task_id=? AND generation=?',
                                             (self.task_id,self.generation)).fetchone()
                        if had and not self.viewers():
                            self.deferred_flush = (self.generation,self.task()['session_id'],
                                                   self.task()['last_completed_turn'])
                        try:
                            peer,_=listener.accept()
                        except socket.timeout:
                            continue
                        with peer:
                            peer.settimeout(2)
                            with peer.makefile('r') as stream:
                                request=json.loads(stream.readline(1024*1024))
                            try:
                                reply=self.command(request)
                            except Exception as exc:
                                reply={'exception':f'{type(exc).__name__}: {exc}'}
                            peer.sendall((json.dumps(reply)+'\n').encode())
                    except TimeoutError:
                        # Close/retry owns the lock; do not flush across it or block shutdown.
                        continue
                    except Exception as exc:
                        self.update(note=f'app-server supervisor: {type(exc).__name__}: {exc}')
                        time.sleep(.2)
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
