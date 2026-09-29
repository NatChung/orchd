"""Explicit, persistent task worktrees. Never reset, reuse, or clean up; integration is explicit."""
import argparse
import datetime
import re
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import uuid


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def schema(con):
    con.executescript('''
    CREATE TABLE IF NOT EXISTS managed_worktrees(
      id TEXT PRIMARY KEY, task_id TEXT NOT NULL, repo TEXT NOT NULL,
      path TEXT NOT NULL UNIQUE, base_ref TEXT NOT NULL, base_revision TEXT NOT NULL,
      branch TEXT NOT NULL, common_dir TEXT NOT NULL, state TEXT NOT NULL,
      error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
      UNIQUE(task_id,common_dir), UNIQUE(common_dir,branch));
    CREATE TABLE IF NOT EXISTS worktree_checkpoints(
      id TEXT PRIMARY KEY, worktree_id TEXT NOT NULL, revision TEXT NOT NULL,
      diff TEXT NOT NULL, untracked TEXT NOT NULL, created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS resource_claims(resource TEXT PRIMARY KEY, attempt_id TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS worktree_operations(
      id TEXT PRIMARY KEY, worktree_id TEXT NOT NULL, kind TEXT NOT NULL,
      inputs TEXT NOT NULL, start_revision TEXT, result_revision TEXT,
      state TEXT NOT NULL, exit_code INTEGER, evidence TEXT,
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
    ''')
    if 'purpose' not in {r[1] for r in con.execute('PRAGMA table_info(managed_worktrees)')}:
        con.execute("ALTER TABLE managed_worktrees ADD COLUMN purpose TEXT NOT NULL DEFAULT 'task'")
        con.commit()
    columns = {r[1] for r in con.execute('PRAGMA table_info(worktree_operations)')}
    for name, kind in [('owner_pid', 'INTEGER'), ('owner_start', 'TEXT')]:
        if name not in columns:
            con.execute('ALTER TABLE worktree_operations ADD COLUMN ' + name + ' ' + kind)
    con.commit()


def git_environment():
    return {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}


def git(repo, *args):
    result = subprocess.run(['git', '-C', str(repo), *args], capture_output=True, text=True, env=git_environment())
    if result.returncode:
        # Git stderr can contain file contents or hook output; persist only operation.
        raise RuntimeError('git %s failed (exit %s)' % (args[0], result.returncode))
    return result.stdout.rstrip('\n')


def identity(repo):
    root = Path(git(repo, 'rev-parse', '--show-toplevel')).resolve()
    common = Path(git(repo, 'rev-parse', '--path-format=absolute', '--git-common-dir')).resolve()
    return str(root), str(common)


def get(con, wid):
    row = con.execute('SELECT * FROM managed_worktrees WHERE id=?', (wid,)).fetchone()
    if row is None:
        raise RuntimeError('unknown managed worktree')
    return dict(row)


def create(con, task_id, repo, base, path, branch, integration=False):
    repo = str(Path(repo).resolve())
    root, common = identity(repo)
    if root != repo:
        raise RuntimeError('--repo must be the exact Git working-tree root')
    if not con.execute('SELECT 1 FROM tasks WHERE id=?', (task_id,)).fetchone():
        raise RuntimeError('unknown task')
    if 'project' in {r[1] for r in con.execute('PRAGMA table_info(tasks)')} and con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='projects'").fetchone():
        project = con.execute('SELECT project FROM tasks WHERE id=?', (task_id,)).fetchone()[0]
        if con.execute('SELECT 1 FROM projects WHERE id=?', (project,)).fetchone() and not con.execute(
                'SELECT 1 FROM project_repos WHERE project_id=? AND identity=?', (project, common)).fetchone():
            raise RuntimeError('repo does not belong to the registered task project')
    git(repo, 'check-ref-format', '--branch', branch)
    if branch.startswith('-') or branch == 'HEAD':
        raise RuntimeError('invalid dedicated branch')
    revision = git(repo, 'rev-parse', '--verify', '--end-of-options', base + '^{commit}')
    target = Path(path).absolute()
    if target.is_symlink():
        raise RuntimeError('worktree path must not be a symlink')
    target = str(target.resolve())
    if os.path.lexists(target):
        raise RuntimeError('worktree path already exists; ownership is unknown')
    # Capture current Git state without modifying it; dirty source trees are allowed.
    git(repo, 'status', '--porcelain=v1', '-z')
    existing = git(repo, 'worktree', 'list', '--porcelain')
    if 'worktree ' + target in existing.splitlines():
        raise RuntimeError('path already registered with Git')
    if git(repo, 'for-each-ref', '--format=%(refname)', 'refs/heads/' + branch).splitlines():
        raise RuntimeError('branch already exists; ownership is unknown')
    wid = 'wt-' + uuid.uuid4().hex
    ts = now()
    try:
        con.execute('BEGIN IMMEDIATE')
        con.execute('''INSERT INTO managed_worktrees
          (id,task_id,repo,path,base_ref,base_revision,branch,common_dir,state,created_at,updated_at)
          VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                    (wid, task_id, repo, target, base, revision, branch, common, 'creating', ts, ts))
        con.execute('UPDATE managed_worktrees SET purpose=? WHERE id=?', ('integration' if integration else 'task', wid))
        con.commit()
    except sqlite3.IntegrityError as error:
        con.rollback()
        raise RuntimeError('task/repo, branch or path already reserved; inspect existing worktree') from error
    try:
        # Git uses the pinned commit even if the base ref advances after reservation.
        if os.path.lexists(target):
            raise RuntimeError('worktree path appeared after reservation')
        git(repo, 'worktree', 'add', '-b', branch, '--', target, revision)
        if identity(target) != (target, common):
            raise RuntimeError('created worktree identity mismatch')
        if Path(repo, '.codegraph').is_dir():
            tool = Path.home() / '.local/bin/codegraph'
            result = subprocess.run([str(tool), 'sync'], cwd=target, capture_output=True, env=git_environment())
            if result.returncode:
                raise RuntimeError('codegraph sync failed; inspect reserved worktree')
        con.execute('UPDATE managed_worktrees SET state=?,updated_at=? WHERE id=?', ('ready', now(), wid))
        con.commit()
    except (OSError, RuntimeError, subprocess.SubprocessError):
        # Any failure after I/O may have created a branch or worktree. Retain reservation.
        con.execute('UPDATE managed_worktrees SET state=?,error=?,updated_at=? WHERE id=?',
                    ('needs_inspection', 'creation did not finish; inspect Git and path before recovery', now(), wid))
        con.commit()
        raise RuntimeError('worktree %s needs inspection; reservation retained' % wid)
    return get(con, wid)


def checkpoint(con, wid):
    row = get(con, wid)
    if row['state'] != 'ready':
        raise RuntimeError('worktree is not ready')
    path = row['path']
    if identity(path) != (path, row['common_dir']):
        raise RuntimeError('worktree identity changed')
    if git(path, 'symbolic-ref', '--short', 'HEAD') != row['branch']:
        raise RuntimeError('worktree branch changed')
    revision = git(path, 'rev-parse', 'HEAD')
    # Stored locally for handoff; never print diff or file contents to the terminal.
    diff = git(path, 'diff', '--no-ext-diff', '--no-textconv', '--binary', 'HEAD', '--')
    untracked = git(path, 'ls-files', '--others', '--exclude-standard', '-z').split('\0')
    untracked = [name for name in untracked if name]
    cid = 'checkpoint-' + uuid.uuid4().hex
    con.execute('INSERT INTO worktree_checkpoints VALUES(?,?,?,?,?,?)',
                (cid, wid, revision, diff, json.dumps(untracked), now()))
    con.commit()
    return {'id': cid, 'worktree_id': wid, 'revision': revision,
            'has_diff': bool(diff), 'untracked_count': len(untracked),
            'consistency': 'best-effort; pause writers before using as a handoff'}


def register_parser(subparsers, ctl):
    sp = subparsers.add_parser('worktree', help='explicit task worktrees; no automatic cleanup').add_subparsers(dest='worktree_action', required=True)
    x = sp.add_parser('create')
    for name in ('task', 'repo', 'base', 'path', 'branch'):
        x.add_argument('--' + name, required=True)
    x.add_argument('--integration', action='store_true', help='dedicated integration branch')
    x.set_defaults(func=lambda a: run(ctl, a))
    x = sp.add_parser('integrate')
    x.add_argument('id')
    x.add_argument('--revision', action='append', required=True)
    x.set_defaults(func=lambda a: run(ctl, a))
    x = sp.add_parser('recover', help='after manual resolution; operator asserts descendant writers stopped')
    x.add_argument('operation')
    x.add_argument('--evidence', required=True)
    x.set_defaults(func=lambda a: run(ctl, a))
    x = sp.add_parser('verify')
    x.add_argument('id')
    x.add_argument('command', nargs=argparse.REMAINDER)
    x.set_defaults(func=lambda a: run(ctl, a))
    for name in ('status', 'checkpoint'):
        x = sp.add_parser(name)
        x.add_argument('id')
        x.set_defaults(func=lambda a: run(ctl, a))
    x = sp.add_parser('list')
    x.add_argument('--task')
    x.set_defaults(func=lambda a: run(ctl, a))


def run(ctl, args):
    con = ctl.db()
    try:
        if args.worktree_action == 'create':
            result = create(con, args.task, args.repo, args.base, args.path, args.branch, args.integration)
        elif args.worktree_action == 'integrate':
            result = integrate(con, args.id, args.revision)
        elif args.worktree_action == 'recover':
            result = recover(con, args.operation, args.evidence)
        elif args.worktree_action == 'verify':
            result = verify(con, args.id, args.command)
        elif args.worktree_action == 'checkpoint':
            result = checkpoint(con, args.id)
        elif args.worktree_action == 'status':
            result = get(con, args.id)
            result['operations'] = [dict(r) for r in con.execute('SELECT id,kind,start_revision,result_revision,state,exit_code,created_at,updated_at FROM worktree_operations WHERE worktree_id=? ORDER BY created_at', (args.id,))]
        else:
            rows = con.execute('SELECT * FROM managed_worktrees' + (' WHERE task_id=?' if args.task else '') + ' ORDER BY created_at', (args.task,) if args.task else ())
            result = [dict(row) for row in rows]
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        con.close()


def reserve_operation(con, wid, kind, inputs):
    """Share cwd claims with the scheduler; interruption never releases a writer."""
    oid = 'wt-op-' + uuid.uuid4().hex
    owner_pid = os.getpid()
    owner_start = process_start(owner_pid)
    if not owner_start:
        raise RuntimeError('cannot establish operation owner process identity')
    try:
        con.execute('BEGIN IMMEDIATE')
        row = get(con, wid)
        if row['state'] != 'ready':
            raise RuntimeError('worktree is not ready')
        if kind == 'integrate' and row['purpose'] != 'integration':
            raise RuntimeError('integration requires a dedicated --integration worktree')
        if row['path'] == row['repo']:
            raise RuntimeError('operations must use a dedicated managed worktree')
        if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='recovery_jobs'").fetchone():
            import scheduling
            if scheduling.recovery_cwd(con, row['path']):
                raise RuntimeError('recovery owns this working directory')
        reject_active_dispatch(con, row['path'])
        con.execute('INSERT INTO resource_claims VALUES(?,?)', ('cwd:' + row['path'], oid))
        con.execute('INSERT INTO worktree_operations(id,worktree_id,kind,inputs,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                    (oid, wid, kind, json.dumps(inputs), 'running', now(), now()))
        con.execute('UPDATE worktree_operations SET owner_pid=?,owner_start=? WHERE id=?', (owner_pid, owner_start, oid))
        con.commit()
    except sqlite3.IntegrityError as error:
        con.rollback()
        raise RuntimeError('worktree already has an active writer or unresolved operation') from error
    except Exception:
        con.rollback()
        raise
    return oid, row


def validate_operation_tree(row):
    path = row['path']
    if identity(path) != (path, row['common_dir']):
        raise RuntimeError('managed worktree identity changed')
    if git(path, 'symbolic-ref', '--short', 'HEAD') != row['branch']:
        raise RuntimeError('managed branch changed')
    if git(path, 'status', '--porcelain=v1', '--untracked-files=all'):
        raise RuntimeError('operation requires a clean working tree including untracked files')
    for name in ('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'rebase-merge', 'rebase-apply'):
        location = git(path, 'rev-parse', '--git-path', name)
        if Path(path, location).exists():
            raise RuntimeError('an existing Git operation needs attention')
    return git(path, 'rev-parse', 'HEAD')


def finish_operation(con, oid, state, revision=None, exit_code=None, evidence=None, release=False):
    con.execute('UPDATE worktree_operations SET state=?,result_revision=?,exit_code=?,evidence=?,updated_at=? WHERE id=?',
                (state, revision, exit_code, evidence, now(), oid))
    if release:
        con.execute('DELETE FROM resource_claims WHERE attempt_id=?', (oid,))
    con.commit()
    return {'id': oid, 'state': state, 'revision': revision, 'exit_code': exit_code,
            'writer_claim_retained': not release}


def integrate(con, wid, revisions):
    # Require full object identities: never silently resolve a moving branch name.
    if not revisions or any(not re.fullmatch(r'[0-9a-fA-F]{40}|[0-9a-fA-F]{64}', r) for r in revisions):
        raise RuntimeError('each --revision must be a full commit object ID')
    oid, row = reserve_operation(con, wid, 'integrate', revisions)
    started = False
    try:
        start = validate_operation_tree(row)
        pinned = [git(row['path'], 'rev-parse', '--verify', r + '^{commit}') for r in revisions]
        if any(a.lower() != b for a, b in zip(revisions, pinned)):
            raise RuntimeError('inputs must identify commits directly, not tag objects')
        con.execute('UPDATE worktree_operations SET start_revision=?,inputs=? WHERE id=?', (start, json.dumps(pinned), oid))
        con.commit()
        started = True
        for revision in pinned:
            git(row['path'], '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgSign=false',
                'merge', '--no-edit', '--no-ff', '--no-gpg-sign', revision)
        final = git(row['path'], 'rev-parse', 'HEAD')
        return finish_operation(con, oid, 'merged', final, 0, 'all pinned inputs merged; tests require explicit verify', True)
    except (OSError, RuntimeError, subprocess.SubprocessError):
        state = 'needs_inspection' if started else 'rejected'
        finish_operation(con, oid, state, evidence='inspect operation and Git state; no reset or cleanup performed', release=not started)
        raise RuntimeError('integration %s %s; inspect worktree status' % (oid, state))


def verify(con, wid, command):
    command = list(command)
    if command and command[0] == '--':
        command.pop(0)
    if not command:
        raise RuntimeError('verify requires an explicit command argv after --')
    oid, row = reserve_operation(con, wid, 'verify', command)
    started = False
    try:
        start = validate_operation_tree(row)
        con.execute('UPDATE worktree_operations SET start_revision=? WHERE id=?', (start, oid))
        con.commit()
        started = True
        # No shell interpolation, guessed commands or terminal output containing secrets.
        result = subprocess.run(command, cwd=row['path'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=git_environment())
        final = git(row['path'], 'rev-parse', 'HEAD')
        dirty = bool(git(row['path'], 'status', '--porcelain=v1', '--untracked-files=all'))
        state = 'passed' if result.returncode == 0 and final == start and not dirty else 'failed'
        return finish_operation(con, oid, state, final, result.returncode,
                                json.dumps({'same_revision': final == start, 'dirty_after': dirty,
                                            'output': 'not retained', 'acceptance': 'operator decision required',
                                            'process_scope': 'foreground command only; explicit commands must wait for their descendants'}), True)
    except (OSError, RuntimeError, subprocess.SubprocessError):
        finish_operation(con, oid, 'needs_inspection' if started else 'rejected', release=not started)
        raise RuntimeError('verification %s needs inspection' % oid)


def process_start(pid):
    result = subprocess.run(['ps', '-p', str(pid), '-o', 'stat=,lstart='],
                            capture_output=True, text=True, env=dict(os.environ, LC_ALL='C'))
    fields = result.stdout.strip().split(None, 1)
    if result.returncode or len(fields) != 2 or fields[0].startswith('Z'):
        return None
    return fields[1]


def reject_active_dispatch(con, path):
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'execution_attempts' in tables:
        claimed = con.execute('SELECT a.cwd FROM resource_claims r JOIN execution_attempts a ON a.id=r.attempt_id')
        if any(os.path.realpath(r[0]) == path for r in claimed):
            raise RuntimeError('worktree has another claimed execution writer')
    if not {'workers', 'messages'} <= tables:
        return
    rows = con.execute("SELECT w.cwd FROM messages m JOIN workers w ON w.id=m.recipient "
                       "WHERE m.kind='dispatch' AND m.state IN "
                       "('queued','sending','sent','acked','unconfirmed','interrupted')")
    if any(os.path.realpath(r[0]) == path for r in rows):
        raise RuntimeError('worktree has an active or unresolved worker dispatch')


def recover(con, oid, evidence):
    """Bounded owner check plus operator assertion; not proof all descendants stopped."""
    if not evidence.strip():
        raise RuntimeError('recovery requires evidence that all descendant writers stopped')
    try:
        con.execute('BEGIN IMMEDIATE')
        operation = con.execute('SELECT * FROM worktree_operations WHERE id=?', (oid,)).fetchone()
        if operation is None or operation['state'] not in ('running', 'needs_inspection'):
            raise RuntimeError('operation is not recoverable')
        if operation['state'] == 'running':
            if not operation['owner_pid'] or not operation['owner_start']:
                raise RuntimeError('operation owner identity is unknown; recovery requires inspection')
            if process_start(operation['owner_pid']) == operation['owner_start']:
                raise RuntimeError('operation owner is still running')
        row = get(con, operation['worktree_id'])
        claim = con.execute('SELECT attempt_id FROM resource_claims WHERE resource=?', ('cwd:' + row['path'],)).fetchone()
        if claim is None or claim[0] != oid:
            raise RuntimeError('worktree claim does not belong to this operation')
        reject_active_dispatch(con, row['path'])
        revision = validate_operation_tree(row)
        # This does not mark merge/test success or task acceptance.
        return finish_operation(con, oid, 'recovered', revision, evidence=json.dumps({
            'operator_evidence': evidence,
            'limitation': 'owner PID check and operator assertion; descendant writers are not independently proven stopped',
            'verification': 'separate explicit verify required'}), release=True)
    except Exception:
        con.rollback()
        raise
