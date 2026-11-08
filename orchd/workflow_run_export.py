"""Export one task's ordered run as scoped offline evidence, without inventing events."""
import hashlib
import os
from pathlib import Path
import re
import subprocess

from . import workflows

KINDS = ('dispatch', 'ack', 'report', 'close', 'verify_lock', 'verification')


def _digest(raw):
    return 'sha256:' + hashlib.sha256(raw).hexdigest()


def _safe(path):
    path = Path(path).absolute()
    if '..' in path.parts:
        raise ValueError('Workflow export paths must not contain parent traversal')
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError('Workflow export paths must not cross symlinks')
    return path


def _destination(task, output_dir, env):
    if env.get('ORCHD_TASK_ID') and env['ORCHD_TASK_ID'] != task['id']:
        raise ValueError('Workflow export task does not match the worker identity')
    if not task['worktree']:
        raise ValueError('Workflow export needs its task worktree')
    worktree = _safe(task['worktree'])
    if env.get('ORCHD_WORKTREE') and _safe(env['ORCHD_WORKTREE']) != worktree:
        raise ValueError('Workflow export worktree does not match the worker identity')
    if not worktree.is_dir():
        raise ValueError('Workflow export task worktree is unavailable')
    requested = Path(output_dir)
    output = _safe(requested if requested.is_absolute() else worktree / requested)
    if output == worktree or not output.is_relative_to(worktree):
        raise ValueError('Workflow export output must be inside its task worktree')
    if output.exists() or not output.parent.is_dir():
        raise ValueError('Workflow export needs a new directory with an existing parent')
    relative = output.relative_to(worktree).as_posix()
    if '.git' in output.relative_to(worktree).parts:
        raise ValueError('Workflow export output must not enter Git metadata')
    ignored = subprocess.run(['git', '-C', str(worktree), 'check-ignore', '--no-index', '--quiet', '--', relative + '/'],
                             capture_output=True, check=False)
    if ignored.returncode != 0:
        raise ValueError('Workflow export output directory must be ignored')
    return output


def _load(raw):
    return workflows.load(raw)


def _inputs(node, plan, authors, missing, stage_id):
    result = {}
    for name, binding in node['inputs'].items():
        value = None
        if set(binding) == {'context'}:
            value = plan['context'].get(binding['context'])
        elif set(binding) == {'output'}:
            source = binding['output']
            attempt = authors.get(source['step'])
            if attempt and attempt['output'] is not None:
                values = _load(attempt['output'])['values']
                value = values.get(source['field'])
        elif set(binding) == {'task'}:
            source = binding['task']
            attempt = authors.get(source['step'])
            if attempt:
                value = attempt['task_id'] if source['field'] == 'task_id' else attempt['worktree']
        if isinstance(value, str):
            result[name] = _digest(value.encode('utf-8'))
        else:
            missing.append(dict(stage_id=stage_id, input=name, reason='binding_not_available_at_export'))
    return result


def _output_values(raw):
    if raw is None:
        return None
    value = _load(raw)
    if (not isinstance(value, dict) or set(value) != {'schema_version', 'values'}
            or value['schema_version'] != 'orchd-workflow-output/v1'
            or not isinstance(value['values'], dict)
            or not all(isinstance(key, str) and isinstance(item, str) for key, item in value['values'].items())):
        raise ValueError('Stored workflow output envelope is invalid')
    return value['values']


def _snapshot(con, task_id):
    # A read savepoint gives all rows the same SQLite snapshot without committing caller work.
    con.execute('SAVEPOINT workflow_evidence_export')
    try:
        task = con.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
        found = con.execute('SELECT run_id FROM workflow_run_attempts WHERE task_id=?', (task_id,)).fetchall()
        if task is None or len(found) != 1:
            raise ValueError('Task must belong to exactly one workflow run')
        run = con.execute('SELECT * FROM workflow_runs WHERE id=?', (found[0]['run_id'],)).fetchone()
        if run is None:
            raise ValueError('Workflow run is unavailable')
        attempts = [dict(row) for row in con.execute(
            'SELECT a.*,t.repo,t.worktree,t.status AS task_status FROM workflow_run_attempts a '
            'LEFT JOIN tasks t ON t.id=a.task_id WHERE a.run_id=? ORDER BY a.step_index,a.role', (run['id'],))]
        messages = [dict(row) for row in con.execute(
            'SELECT m.id,m.task_id,m.kind,m.body,m.evidence,m.created_at,t.repo '
            'FROM messages m JOIN tasks t ON t.id=m.task_id '
            'JOIN workflow_run_attempts a ON a.task_id=m.task_id '
            'WHERE a.run_id=? AND m.kind IN (' + ','.join('?' for _ in KINDS) + ') ORDER BY m.id',
            (run['id'], *KINDS))]
        return dict(task), dict(run), attempts, messages
    finally:
        con.execute('RELEASE SAVEPOINT workflow_evidence_export')


def export_for_task(con, task_id, output_dir, *, env=None):
    """Write new ignored evidence files for the run containing this task; never read other chats."""
    if not isinstance(task_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', task_id):
        raise ValueError('Invalid workflow export task ID')
    env = os.environ if env is None else env
    if env.get('ORCHD_TASK_ID') and env['ORCHD_TASK_ID'] != task_id:
        raise ValueError('Workflow export task does not match the worker identity')
    task_hint = con.execute('SELECT id,worktree FROM tasks WHERE id=?', (task_id,)).fetchone()
    if task_hint is None:
        raise ValueError('Task must belong to exactly one workflow run')
    output = _destination(dict(task_hint), output_dir, env)
    task, run, attempts, messages = _snapshot(con, task_id)
    if task['worktree'] != task_hint['worktree']:
        raise ValueError('Workflow export task worktree changed during inspection')
    plan = _load(run['plan'])
    authors = {plan['steps'][a['step_index']]['id']: a for a in attempts if a['role'] == 'author'}
    by_role = {(a['step_index'], a['role']): a for a in attempts}
    stages, missing, transitions = [], [], []
    previous = None
    for index, node in enumerate(plan['steps']):
        author_id = f'step-{index:03d}-author'
        for role, spec in [('author', node), *([('reviewer', node['verification'])] if 'verification' in node else [])]:
            stage_id = author_id if role == 'author' else f'step-{index:03d}-reviewer'
            attempt = by_role.get((index, role))
            stage = dict(id=stage_id, workflow_stage=spec['stage'], repository=spec['repo'],
                         task_ids=[attempt['task_id']] if attempt else [])
            predecessor = previous if role == 'author' else author_id
            if predecessor is not None:
                transitions.append(dict(source=predecessor, target=stage_id,
                                        gate='author_report' if role == 'reviewer' else
                                             'report_and_native_verification' if previous.endswith('-reviewer') else 'task_report',
                                        audit_support='not_checked_by_close_based_auditor'))
            digests = _inputs(spec, plan, authors, missing, stage_id)
            if digests:
                stage['input_digests'] = digests
            stages.append(stage)
        previous = stages[-1]['id']
    audit_plan = dict(schema_version='workflow-run-plan/v1', run_id=run['id'], workflow=plan['workflow'], stages=stages)
    state = dict(schema_version='orchd-run-export/v1', run_id=run['id'], exported_by_task=task_id,
                 state=run['state'], step_index=run['step_index'], version=run['version'],
                 workflow=plan['workflow'], plan_digest=run['plan_digest'],
                 attempts=[{key: a[key] for key in ('step_index', 'role', 'task_id', 'state', 'repo', 'task_status',
                                                   'output_digest', 'report_id', 'lock_id')} |
                           dict(worktree=a['worktree'], output_values=_output_values(a['output']),
                                output_raw_digest=_digest(a['output']) if a['output'] is not None else None) for a in attempts],
                 unresolved_inputs=missing, expected_transitions=transitions,
                 frozen_plan=plan,
                 limitations=['Snapshot of supplied run evidence, not acceptance or method compliance.',
                              'The exporting task may not yet have a result; absent events are not synthesized.',
                              'Ordered transitions use report/native verification; close-based audit dependency edges are intentionally omitted.',
                              'Output values are opaque task declarations, not permission to read referenced paths.'])
    files = {'auditplan.json': workflows.canonical(audit_plan),
             'core.jsonl': b''.join(workflows.canonical(row) for row in messages),
             'run.json': workflows.canonical(state)}
    receipt = dict(schema_version='orchd-run-export-receipt/v1', run_id=run['id'], task_id=task_id,
                   plan_digest=run['plan_digest'], files={name: _digest(raw) for name, raw in files.items()},
                   message_count=len(messages), last_message_id=messages[-1]['id'] if messages else None,
                   output_dir=str(output), basis='Real scoped task messages; task results remain self-reported.')
    # New directory and exclusive files preserve any prior export, including partial runs.
    output.mkdir(mode=0o700)
    for name, raw in {**files, 'receipt.json': workflows.canonical(receipt)}.items():
        with (output / name).open('xb') as stream:
            stream.write(raw)
    return receipt
