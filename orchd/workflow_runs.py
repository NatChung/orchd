"""Profile-local ordered workflow runs. Plugin text never grants runtime permissions."""
import contextlib
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time
import uuid

from . import paths, store, workflows
from .file_lock import try_lock

SCHEMA = 'orchd-run-plan/v1'
OUTPUT_SCHEMA = 'orchd-workflow-output/v1'
@dataclass(frozen=True)
class _Reservation:
    attempt_id: str


MAX_OUTPUT = 256 * 1024
DIGEST = re.compile(r'sha256:[0-9a-f]{64}')


def _json(raw):
    return workflows.load(raw)


def _hash(raw):
    return 'sha256:' + hashlib.sha256(raw).hexdigest()


def _event(con, run_id, kind, body, task_id=None, message_id=None):
    con.execute('INSERT OR IGNORE INTO workflow_run_events(run_id,kind,task_id,source_message_id,body,created_at) '
                'VALUES(?,?,?,?,?,?)', (run_id, kind, task_id, message_id, json.dumps(body), time.time()))


def _get(con, run_id):
    row = con.execute('SELECT * FROM workflow_runs WHERE id=?', (run_id,)).fetchone()
    if row is None:
        raise ValueError('Unknown workflow run')
    return row


def _state(con, run_id, state, note=None):
    con.execute("UPDATE workflow_runs SET state=?,note=?,version=version+1,updated_at=? WHERE id=? AND (state NOT IN ('cancelling','cancelled') OR ? IN ('cancelling','cancelled'))",
                (state, note, time.time(), run_id, state))


def _plugin(pin):
    if not isinstance(pin, dict) or set(pin) != {'name', 'commit', 'digest'}:
        raise ValueError('Run needs an exact workflow pin')
    workflows.name(pin['name'])
    if not isinstance(pin['commit'], str) or not workflows.SHA.fullmatch(pin['commit']) or not isinstance(pin['digest'], str) or not DIGEST.fullmatch(pin['digest']):
        raise ValueError('Run needs a full workflow commit and receipt digest')
    manifest, files, receipt = workflows.cached(workflows.root() / pin['name'] / pin['commit'], pin['digest'])
    if manifest['name'] != pin['name'] or receipt['commit'] != pin['commit'] or receipt['name'] != pin['name']:
        raise ValueError('Workflow pin identity mismatch')
    return manifest, files


def _repo(rt, name):
    workflows.repo_name(name)
    path = Path(rt.repo_path(name))
    if path.name != name:
        raise ValueError('Run repo must be the registered repository name, not a path alias')
    return str(path.resolve())


def _binding(repo, workflow):
    policy = workflows.bindings()
    bound = policy['repos'].get(repo) or policy['default']
    if bound is not None and bound != workflow:
        raise ValueError('Run workflow differs from the profile repository binding')


def _validate(plan, rt, _parent_outputs=None):
    from .core import TASK_TYPES
    if not isinstance(plan, dict) or set(plan) - {'schema_version', 'workflow', 'context', 'steps', 'limits', 'failure_steps'} or not {'schema_version', 'workflow', 'context', 'steps'} <= plan.keys() or plan['schema_version'] != SCHEMA:
        raise ValueError('Invalid ordered workflow run plan')
    manifest, _ = _plugin(plan['workflow'])
    context = plan['context']
    if not isinstance(context, dict) or not all(isinstance(k, str) and workflows.PARAM.fullmatch(k) and isinstance(v, str) for k, v in context.items()):
        raise ValueError('Run context must map parameter names to exact strings')
    steps = plan['steps']
    if not isinstance(steps, list) or not 1 <= len(steps) <= 32:
        raise ValueError('Run needs 1 to 32 ordered steps')
    limits = plan.get('limits', {})
    if not isinstance(limits, dict) or set(limits) - {'deadline_seconds', 'max_attempts'}:
        raise ValueError('Invalid workflow run limits')
    if 'deadline_seconds' in limits and (type(limits['deadline_seconds']) is not int or not 1 <= limits['deadline_seconds'] <= 604800):
        raise ValueError('Invalid run deadline')
    if 'max_attempts' in limits and (type(limits['max_attempts']) is not int or limits['max_attempts'] != 1):
        raise ValueError('This version permits one attempt per step; recovery never blindly respawns')
    known = dict(_parent_outputs or {})
    repositories = {}
    required = {'repo', 'stage', 'task_type', 'model', 'model_reason', 'instructions', 'done_when', 'inputs'}
    def node(value, available, verifier=False):
        extra = {'paths', 'command', 'outputs'} if verifier else {'id', 'outputs', 'title', 'base_branch', 'backend', 'verification'}
        if not isinstance(value, dict) or not required <= value.keys() or value.keys() - required - extra:
            raise ValueError('Invalid workflow step fields')
        repositories[value['repo']] = _repo(rt, value['repo'])
        _binding(value['repo'], plan['workflow']['name'])
        workflows.name(value['stage'])
        selected = manifest['stages'].get(value['stage'])
        if selected is None or value['task_type'] not in TASK_TYPES or value['task_type'] != selected['task_type']:
            raise ValueError('Run stage and task_type must match the pinned workflow')
        for key in ('model', 'model_reason', 'instructions', 'done_when'):
            if not isinstance(value[key], str) or not value[key].strip():
                raise ValueError('Run dispatch fields must be nonempty strings')
        if not isinstance(value['inputs'], dict) or set(value['inputs']) != set(selected['inputs']):
            raise ValueError('Run inputs must exactly match declared stage inputs')
        for ref in value['inputs'].values():
            if isinstance(ref, dict) and set(ref) == {'context'} and isinstance(ref['context'], str) and ref['context'] in context:
                continue
            if isinstance(ref, dict) and set(ref) == {'task'}:
                source = ref['task']
                if isinstance(source, dict) and set(source) == {'step', 'field'} and isinstance(source['step'], str) and source['step'] in available and source['field'] in ('task_id', 'worktree'):
                    continue
            if isinstance(ref, dict) and set(ref) == {'output'}:
                source = ref['output']
                if isinstance(source, dict) and set(source) == {'step', 'field'} and isinstance(source['step'], str) and source['step'] in available and source['field'] in available[source['step']]:
                    continue
            raise ValueError('Inputs must reference fixed context or an earlier declared output')
        outputs = value.get('outputs', [])
        if not isinstance(outputs, list) or not all(isinstance(k, str) and workflows.PARAM.fullmatch(k) for k in outputs) or len(set(outputs)) != len(outputs):
            raise ValueError('Outputs must be unique parameter names')
        if verifier:
            if value['task_type'] != 'review' or not isinstance(value.get('command'), str) or not value['command'].strip() or not isinstance(value.get('paths'), list) or not value['paths'] or not all(isinstance(p, str) and p for p in value['paths']):
                raise ValueError('Verification needs a review stage, locked paths and a fixed command')
    for step in steps:
        node(step, known)
        workflows.name(step.get('id'))
        if step['id'] in known or 'outputs' not in step:
            raise ValueError('Run steps need unique IDs and declared outputs')
        known[step['id']] = step['outputs']
        if 'verification' in step:
            node(step['verification'], known, True)
            if step['verification']['repo'] != step['repo']:
                raise ValueError('Verifier must use the author repository')
    if 'failure_steps' in plan:
        failure_steps = plan['failure_steps']
        if not isinstance(failure_steps, list) or not 1 <= len(failure_steps) <= 8 or not isinstance(failure_steps[-1], dict) or 'verification' not in failure_steps[-1] or 'failure' in context:
            raise ValueError('Failure steps need a final independent verifier and a reserved failure context')
        repair = dict(schema_version=SCHEMA, workflow=plan['workflow'], context=dict(context, failure='Core finding context'), steps=failure_steps, limits=limits)
        _, repair_repos = _validate(repair, rt, _parent_outputs=known)
        repositories.update(repair_repos)
    raw = workflows.canonical(plan)
    if len(raw) > 1024 * 1024:
        raise ValueError('Run plan exceeds 1 MiB')
    return raw, repositories


def start(con, rt, *, plan, request_id, orch_thread, idempotency_key):
    """Freeze a plan against an authenticated, owned interface request. No worker spawn here."""
    if type(request_id) is not int or request_id < 1 or not isinstance(idempotency_key, str) or not idempotency_key.strip():
        raise ValueError('Run needs a request ID and idempotency key')
    request = con.execute('SELECT r.*,i.name,i.disabled FROM interface_requests r JOIN interfaces i ON i.id=r.interface_id WHERE r.id=?', (request_id,)).fetchone()
    if request is None or request['orch_id'] != orch_thread:
        raise ValueError('Run origin must be an authenticated request owned by this Orch')
    old = con.execute('SELECT * FROM workflow_runs WHERE request_id=? AND parent_run_id IS NULL', (request_id,)).fetchone()
    if old:
        if old['plan_digest'] != _hash(workflows.canonical(plan)) or old['idempotency_key'] != idempotency_key:
            raise ValueError('Run idempotency conflict')
        return show(con, old['id'])
    if request['disabled'] or request['reply'] is not None or request['dismissed_at'] is not None:
        raise ValueError('Run origin is no longer active')
    raw, repositories = _validate(plan, rt)
    origin = dict(request_id=request_id, interface_id=request['interface_id'], conversation=request['conversation'],
                  request_key=request['request_key'], repositories=repositories)
    with store.immediate(con):
        old = con.execute('SELECT * FROM workflow_runs WHERE request_id=? AND parent_run_id IS NULL', (request_id,)).fetchone()
        if old:
            if old['plan_digest'] != _hash(raw) or old['idempotency_key'] != idempotency_key:
                raise ValueError('Run idempotency conflict')
            return show(con, old['id'])
        run_id = uuid.uuid4().hex[:16]
        now = time.time()
        con.execute('INSERT INTO workflow_runs(id,request_id,orch_thread,idempotency_key,plan,plan_digest,origin,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (run_id, request_id, orch_thread, idempotency_key, raw, _hash(raw), json.dumps(origin), 'active', now, now))
        _event(con, run_id, 'started', dict(plan_digest=_hash(raw), workflow=plan['workflow']))
    return show(con, run_id)


def show(con, run_id):
    run = _get(con, run_id)
    plan = _json(run['plan'])
    attempts = []
    for row in con.execute('SELECT * FROM workflow_run_attempts WHERE run_id=? ORDER BY step_index,role', (run_id,)):
        task = con.execute('SELECT status FROM tasks WHERE id=?', (row['task_id'],)).fetchone()
        attempts.append(dict(step=plan['steps'][row['step_index']]['id'], role=row['role'], task_id=row['task_id'],
                             state=row['state'], task_status=task['status'] if task else None,
                             output_digest=row['output_digest'], report_id=row['report_id'], lock_id=row['lock_id'], note=row['note']))
    repair = con.execute('SELECT id FROM workflow_runs WHERE parent_run_id=?', (run_id,)).fetchone()
    return dict(run_id=run_id, parent_run_id=run['parent_run_id'], repair_run_id=repair['id'] if repair else None, state=run['state'], step_index=run['step_index'], version=run['version'],
                workflow=plan['workflow'], plan_digest=run['plan_digest'], request_id=run['request_id'], note=run['note'],
                result_basis='Task reports are self-reported; verification is evaluated separately.', attempts=attempts)


def events(con, run_id, after=0):
    _get(con, run_id)
    return [dict(row) for row in con.execute('SELECT * FROM workflow_run_events WHERE run_id=? AND id>? ORDER BY id', (run_id, after))]


def _attempt(con, task_id):
    return con.execute('SELECT * FROM workflow_run_attempts WHERE task_id=?', (task_id,)).fetchone()


def _output(task, output_file, expected):
    if isinstance(output_file, os.PathLike):
        output_file = os.fspath(output_file)
    if not isinstance(output_file, str):
        raise ValueError('Workflow report done requires output_file')
    relative = workflows.relative(output_file)
    root = Path(task['worktree']) if task['worktree'] else None
    if root is None:
        raise ValueError('Workflow task has no worktree')
    target = root / relative
    # Check the entire existing chain, including worktree ancestors, before opening.
    for path in (target, *target.parents):
        if path.is_symlink():
            raise ValueError('Workflow output must not cross symlinks')
    if not stat.S_ISREG(target.stat().st_mode) or target.stat().st_size > MAX_OUTPUT:
        raise ValueError('Workflow output must be a regular JSON file under 256 KiB')
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    fd = os.open(target, flags)
    with os.fdopen(fd, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('Workflow output is not a regular file')
        raw = stream.read(MAX_OUTPUT + 1)
    if len(raw) > MAX_OUTPUT:
        raise ValueError('Workflow output exceeds 256 KiB')
    value = _json(raw.decode('utf-8'))
    if not isinstance(value, dict) or set(value) != {'schema_version', 'values'} or value['schema_version'] != OUTPUT_SCHEMA or not isinstance(value['values'], dict) or set(value['values']) != set(expected) or not all(isinstance(v, str) for v in value['values'].values()):
        raise ValueError('Workflow output must contain exactly its declared string values')
    return raw


def guard_callback(con, task_id):
    attempt = _attempt(con, task_id)
    if attempt is None:
        return
    run = _get(con, attempt['run_id'])
    if store.get_task(con, task_id)['status'] == 'closed' or run['state'] in ('cancelling', 'cancelled', 'done', 'needs_adoption'):
        raise ValueError('Terminal workflow tasks cannot reopen through late callbacks')


def _delivered_answer(con, task_id, before=None):
    question = con.execute("SELECT id FROM messages WHERE task_id=? AND kind='question' ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    if question is None:
        return None
    upper = ' AND id<?' if before is not None else ''
    args = (task_id, question['id'], before) if before is not None else (task_id, question['id'])
    answer = con.execute("SELECT id FROM messages WHERE task_id=? AND kind='answer' AND id>?" + upper + ' ORDER BY id DESC LIMIT 1', args).fetchone()
    return dict(question_id=question['id'], answer_id=answer['id']) if answer else None


def _resume_answered_report(con, attempt, message_id):
    run = _get(con, attempt['run_id'])
    if run['state'] != 'awaiting_input':
        return
    proof = _delivered_answer(con, attempt['task_id'], before=message_id)
    if proof is None:
        raise ValueError('Workflow done report needs a delivered answer after the latest question; queued answers are not delivery')
    _state(con, run['id'], 'active')
    _event(con, run['id'], 'answered_report_resumed', dict(proof, report_id=message_id), attempt['task_id'], message_id)
    if run['parent_run_id']:
        parent = _get(con, run['parent_run_id'])
        if parent['state'] == 'awaiting_input':
            _state(con, parent['id'], 'repairing', 'Answered recovery worker continued within the fixed scope')


def prepare_report(con, task_id, status, output_file):
    attempt = _attempt(con, task_id)
    if attempt is None:
        if output_file is not None:
            raise ValueError('output_file is only supported for workflow run tasks')
        return None
    guard_callback(con, task_id)
    spec = _json(attempt['spec'])
    if status != 'done':
        return None
    if _get(con, attempt['run_id'])['state'] == 'awaiting_input' and _delivered_answer(con, task_id) is None:
        raise ValueError('Workflow done report needs a delivered answer after the latest question; queued answers are not delivery')
    if not spec['outputs'] and output_file is None:
        return workflows.canonical(dict(schema_version=OUTPUT_SCHEMA, values={}))
    return _output(store.get_task(con, task_id), output_file, spec['outputs'])


def observe_report(con, *, task_id, message_id, status, output):
    """Called inside core.report's short transaction, with already captured output bytes."""
    attempt = _attempt(con, task_id)
    if attempt is None:
        return
    if attempt['report_id'] is not None:
        if attempt['output'] != output:
            raise ValueError('Workflow task already reported different output')
        if status == 'done':
            _resume_answered_report(con, attempt, message_id)
        return
    con.execute('UPDATE workflow_run_attempts SET state=?,output=?,output_digest=?,report_id=? WHERE id=?',
                ('reported' if status == 'done' else 'blocked', output, _hash(output) if output is not None else None, message_id, attempt['id']))
    _event(con, attempt['run_id'], 'report', dict(status=status, output_digest=_hash(output) if output is not None else None, output_basis='no_declared_outputs_task_self_report' if not _json(attempt['spec'])['outputs'] else 'captured_task_output_schema_validated'), task_id, message_id)
    if status == 'done':
        _resume_answered_report(con, attempt, message_id)
    if status == 'blocked':
        _fail(con, _get(con, attempt['run_id']), 'Worker reported blocked', task_id=task_id, message_id=message_id)


def _blocked(con, run, reason):
    """Every terminal recovery failure reaches the operation that owns its origin."""
    with contextlib.nullcontext() if con.in_transaction else store.immediate(con):
        _state(con, run['id'], 'blocked', reason)
        if run['parent_run_id']:
            _state(con, run['parent_run_id'], 'blocked', 'Bounded recovery failed; no further automatic attempt. ' + reason)
            _event(con, run['parent_run_id'], 'recovery_failed', dict(repair_run_id=run['id'], reason=reason))


def _fail(con, run, reason, *, task_id=None, message_id=None, verification=None):
    """At most one fixed recovery run; a recovery failure never recursively starts another."""
    plan = _json(run['plan'])
    with contextlib.nullcontext() if con.in_transaction else store.immediate(con):
        if _get(con, run['id'])['state'] in ('cancelling', 'cancelled'):
            return
        if run['parent_run_id'] and _get(con, run['parent_run_id'])['state'] in ('cancelling', 'cancelled'):
            return
        captured, provenance, task_refs = {}, {}, []
        for attempt in con.execute('SELECT * FROM workflow_run_attempts WHERE run_id=? ORDER BY step_index,role', (run['id'],)):
            step_id = plan['steps'][attempt['step_index']]['id']
            task = con.execute('SELECT worktree FROM tasks WHERE id=?', (attempt['task_id'],)).fetchone()
            task_refs.append(dict(step=step_id, role=attempt['role'], task_id=attempt['task_id'], worktree=task['worktree'] if task else None))
            if attempt['role'] == 'author' and attempt['output'] is not None:
                captured[step_id] = _json(attempt['output'])['values']
                provenance[step_id] = dict(task_id=attempt['task_id'], output_digest=attempt['output_digest'], report_id=attempt['report_id'], state=attempt['state'])
        finding = dict(source_run=run['id'], source_task=task_id, source_message_id=message_id,
                       reason=reason, verification=verification, validated_outputs=captured,
                       output_provenance=provenance, task_refs=task_refs,
                       basis='Schema-validated captured task reports, not independent acceptance; absent outputs must not be invented')
        _event(con, run['id'], 'failure', finding, task_id, message_id)
        if run['parent_run_id'] or not plan.get('failure_steps'):
            _blocked(con, run, reason)
            return
        existing = con.execute('SELECT id FROM workflow_runs WHERE parent_run_id=?', (run['id'],)).fetchone()
        if not existing:
            child_id = uuid.uuid4().hex[:16]
            repair_plan = dict(schema_version=SCHEMA, workflow=plan['workflow'],
                               context=dict(plan['context'], failure=json.dumps(finding, sort_keys=True)),
                               steps=plan['failure_steps'], limits=plan.get('limits', {}))
            now = time.time()
            if 'deadline_seconds' in repair_plan['limits']:
                repair_plan['limits'] = dict(repair_plan['limits'], deadline_seconds=max(1, int(run['created_at'] + repair_plan['limits']['deadline_seconds'] - now)))
            raw = workflows.canonical(repair_plan)
            if len(raw) > 1024 * 1024:
                _state(con, run['id'], 'blocked', 'Failure context exceeds the fixed plan limit; evidence retained')
                return
            con.execute('INSERT INTO workflow_runs(id,request_id,parent_run_id,orch_thread,idempotency_key,plan,plan_digest,origin,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                        (child_id, run['request_id'], run['id'], run['orch_thread'], 'bounded-recovery:' + run['id'], raw, _hash(raw), run['origin'], 'active', now, now))
            _event(con, run['id'], 'recovery_started', dict(repair_run_id=child_id))
            _event(con, child_id, 'started', dict(parent_run_id=run['id'], finding=finding))
        _state(con, run['id'], 'repairing', reason)


def observe_verification(con, *, task_id, message_id):
    attempt = _attempt(con, task_id)
    if attempt:
        _event(con, attempt['run_id'], 'verification', {}, task_id, message_id)


def task_origin(con, task_id):
    attempt = _attempt(con, task_id)
    if attempt is None:
        return None
    run = _get(con, attempt['run_id'])
    return dict(json.loads(run['origin']), run_id=run['id'], state=run['state'])


def guard_interface_reply(con, request_id, orch_id):
    """Transport boundary: the engine owns pending run replies, including recovery children."""
    request = con.execute('SELECT orch_id,reply FROM interface_requests WHERE id=?', (request_id,)).fetchone()
    if request is None or request['reply'] is not None:
        return
    if con.execute('SELECT 1 FROM workflow_runs WHERE request_id=? LIMIT 1', (request_id,)).fetchone():
        if request['orch_id'] != orch_id:
            raise ValueError('Workflow request is not owned by this Orch')
        raise ValueError('Workflow engine owns this source route; manual interim/final replies are refused. Use workflow_run_show')


def guard_desktop_route(con, orch_id, task_id=None):
    if task_id is not None:
        task = store.get_task(con, task_id)
        if task['orch_thread'] != orch_id:
            raise ValueError('Desktop route task is not owned by this Orch')
        if task_origin(con, task_id) is not None:
            raise ValueError('Workflow task replies are fixed to their authenticated request origin; Desktop routing is refused')
        return
    if con.execute("SELECT 1 FROM workflow_runs WHERE orch_thread=? AND state NOT IN ('done','needs_adoption','cancelled') LIMIT 1", (orch_id,)).fetchone():
        raise ValueError('This Orch has an external workflow run; unscoped Desktop routing is refused. '
                         'Use interface_reply for an ordinary request at its fixed source, '
                         'or workflow_run_show for a workflow-owned request')


def observe_question(con, *, task_id, message_id):
    attempt = _attempt(con, task_id)
    if attempt:
        _state(con, attempt['run_id'], 'awaiting_input', 'Worker question requires a response on the original request origin')
        _event(con, attempt['run_id'], 'question', {}, task_id, message_id)
        run = _get(con, attempt['run_id'])
        if run['parent_run_id']:
            _state(con, run['parent_run_id'], 'awaiting_input', 'Recovery worker question requires the original request origin')
            _event(con, run['parent_run_id'], 'question', {}, task_id, message_id)


def _notify_attention(con, run):
    run = _get(con, run['id'])
    from . import interface
    if run['state'] not in ('needs_recovery', 'blocked', 'awaiting_input', 'repairing'):
        return
    key = 'origin_notification:' + run['state']
    question = con.execute("SELECT m.id,m.body FROM workflow_run_events e JOIN messages m ON m.id=e.source_message_id WHERE e.run_id=? AND e.kind='question' ORDER BY e.id DESC LIMIT 1", (run['id'],)).fetchone() if run['state'] == 'awaiting_input' else None
    source_id = question['id'] if question else None
    if con.execute('SELECT 1 FROM workflow_run_events WHERE run_id=? AND kind=? AND source_message_id IS ? LIMIT 1', (run['id'], key, source_id)).fetchone():
        return
    origin = json.loads(run['origin'])
    request = con.execute('SELECT * FROM interface_requests WHERE id=?', (run['request_id'],)).fetchone()
    if request is None or any(request[k] != origin[k] for k in ('interface_id', 'conversation', 'request_key')):
        return
    if run['parent_run_id']:
        # Parent failure owns the single external final; children never reroute it.
        _notify_attention(con, _get(con, run['parent_run_id']))
        return
    if run['state'] == 'repairing':
        text = 'Workflow run ' + run['id'] + ' is repairing a recorded failure using its fixed bounded plan. No completion or adoption has been accepted.'
        kind = 'interim'
    elif run['state'] == 'awaiting_input':
        text = 'Workflow run ' + run['id'] + ' awaits input; no new action was authorized.\n' + (question['body'] if question else run['note'] or '')
        if len(text) > 60000:
            text = 'Workflow run ' + run['id'] + ' awaits input. The original question is retained as task message ' + str(source_id) + '; it exceeds this transport response size.'
        kind = 'interim'
    else:
        text = 'Workflow run ' + run['id'] + ' stopped in ' + run['state'] + '. It has not completed. ' + (run['note'] or 'Inspect its retained evidence before recovery.')
        kind = 'final'
    try:
        with _transition_lock(con):
            if _get(con, run['id'])['state'] != run['state']:
                return
            interface.reply(con, request['orch_id'], request_id=run['request_id'], text=text, kind=kind)
        _event(con, run['id'], key, dict(request_id=run['request_id'], kind=kind, text_digest=_hash(text.encode())), message_id=source_id)
    except Exception as error:
        if not con.execute("SELECT 1 FROM workflow_run_events WHERE run_id=? AND kind='origin_delivery_pending' AND source_message_id IS ? LIMIT 1", (run['id'], source_id)).fetchone():
            _event(con, run['id'], 'origin_delivery_pending', dict(error_type=type(error).__name__, request_id=run['request_id']), message_id=source_id)


def guard_close(con, task_id, outcome=None):
    attempt = _attempt(con, task_id)
    if attempt is None:
        return
    run = _get(con, attempt['run_id'])
    spec = _json(attempt['spec'])
    if run['state'] in ('blocked', 'needs_adoption', 'cancelling', 'cancelled') and outcome in ('parked', 'abandoned'):
        return
    if attempt['role'] == 'author' and spec.get('verification'):
        from . import verify
        if attempt['state'] != 'succeeded' or verify.status(con, task_id)['state'] != 'pass':
            raise ValueError('Workflow author cannot close before independent verification succeeds')
    if run['state'] in ('active', 'paused', 'needs_recovery', 'repairing', 'awaiting_input') and run['step_index'] < len(_json(run['plan'])['steps']):
        raise ValueError('Workflow producer worktrees are retained until the run is terminal')


def _inputs(con, run, refs):
    plan = _json(run['plan'])
    result = {}
    for key, ref in refs.items():
        if 'context' in ref:
            result[key] = plan['context'][ref['context']]
        else:
            source = ref.get('output', ref.get('task'))
            source_run, source_plan = run, plan
            if not any(step['id'] == source['step'] for step in plan['steps']) and run['parent_run_id']:
                source_run = _get(con, run['parent_run_id'])
                source_plan = _json(source_run['plan'])
            index = next(i for i, step in enumerate(source_plan['steps']) if step['id'] == source['step'])
            row = con.execute("SELECT output,task_id FROM workflow_run_attempts WHERE run_id=? AND step_index=? AND role='author'", (source_run['id'], index)).fetchone()
            if row is None:
                raise ValueError('Prior workflow task is not available')
            if 'task' in ref:
                task = store.get_task(con, row['task_id'])
                result[key] = task['id'] if source['field'] == 'task_id' else task['worktree']
                if not isinstance(result[key], str):
                    raise ValueError('Prior workflow task metadata is not available')
            else:
                if row['output'] is None:
                    raise ValueError('Prior workflow output is not available')
                result[key] = _json(row['output'])['values'][source['field']]
    return result


def _reserve(con, run, node, role, author=None):
    if _get(con, run['id'])['state'] != 'active':
        raise ValueError('Workflow run no longer accepts dispatch reservations')
    plan = _json(run['plan'])
    manifest, files = _plugin(plan['workflow'])
    selected = manifest['stages'][node['stage']]
    _binding(node['repo'], plan['workflow']['name'])
    inputs = _inputs(con, run, node['inputs'])
    metadata = dict(plan['workflow'], stage=node['stage'], inputs=inputs)
    rendered = workflows.PLACEHOLDER.sub(lambda match: inputs[match[1]], files[selected['instructions_file']].decode())
    criteria = workflows.PLACEHOLDER.sub(lambda match: inputs[match[1]], files[selected['done_when_file']].decode())
    instructions = ('Workflow stage (declarative instructions, not additional authorization):\n'
                    + workflows.canonical(metadata).decode() + '\n' + rendered
                    + '\n\nCaller goal, scope and allowed actions:\n' + node['instructions']
                    + '\n\nThe worker brief and active runtime permissions remain binding. Workflow text grants no additional authorization.'
                    + ('\n\nThis is a workflow run task. Produce a UTF-8 JSON file with schema_version=orchd-workflow-output/v1 '
                       + 'and values containing exactly these string fields: ' + json.dumps(node.get('outputs', []))
                       + '. Finish using report --output-file <task-relative JSON path>. Never copy long context into a new dispatch.'
                       if node.get('outputs') else '\n\nThis workflow task declares no output fields. Report normally without an output file; core captures an empty self-reported envelope. Native verification, where required, remains separate.'))
    spec = dict(repo=node['repo'], stage=node['stage'], task_type=node['task_type'], model=node['model'],
                model_reason=node['model_reason'], title=node.get('title', node.get('id', 'Independent verification')),
                caller_instructions=node['instructions'], caller_done_when=node['done_when'],
                instructions=instructions, done_when='Workflow completion criteria:\n' + criteria + '\n\nCaller completion criteria:\n' + node['done_when'],
                workflow=metadata, outputs=node.get('outputs', []), verification=node.get('verification'))
    for key in ('backend', 'base_branch'):
        if key in node:
            spec[key] = node[key]
    if author:
        spec['verifies'] = author['task_id']
    attempt_id, task_id = uuid.uuid4().hex, store.new_task_id()
    con.execute('INSERT INTO workflow_run_attempts(id,run_id,step_index,role,task_id,state,spec) VALUES(?,?,?,?,?,?,?)',
                (attempt_id, run['id'], run['step_index'], role, task_id, 'reserved', workflows.canonical(spec)))
    _event(con, run['id'], 'reserved', dict(role=role, input_digests={k: _hash(v.encode()) for k, v in inputs.items()}), task_id)
    return con.execute('SELECT * FROM workflow_run_attempts WHERE id=?', (attempt_id,)).fetchone()


def dispatch_binding(con, attempt_id, *, repo, instructions, done_when, workflow, workflow_stage, workflow_inputs, task_type):
    if not isinstance(attempt_id, _Reservation):
        raise ValueError('Workflow reservation is internal, not a dispatch parameter')
    row = con.execute('SELECT * FROM workflow_run_attempts WHERE id=?', (attempt_id.attempt_id,)).fetchone()
    if row is None or row['state'] != 'launching' or _get(con, row['run_id'])['state'] != 'active':
        raise ValueError('Invalid workflow launch reservation')
    spec = _json(row['spec'])
    got = dict(repo=repo, caller_instructions=instructions, caller_done_when=done_when, task_type=task_type,
               workflow=dict(spec['workflow'], name=workflow, stage=workflow_stage, inputs=workflow_inputs))
    if any(spec[key] != value for key, value in got.items()):
        raise ValueError('Workflow dispatch changed its reserved repository or input bytes')
    return row['task_id'], spec['instructions'], spec['done_when'], spec['workflow']


def _launch(con, rt, run, attempt):
    from . import core
    spec = _json(attempt['spec'])
    with store.immediate(con):
        if _get(con, run['id'])['state'] != 'active':
            return
        changed = con.execute("UPDATE workflow_run_attempts SET state='launching' WHERE id=? AND state='reserved'", (attempt['id'],)).rowcount
        if changed != 1:
            return
    try:
        core.dispatch(con, rt, orch_thread=run['orch_thread'], repo=spec['repo'], title=spec['title'],
                      instructions=spec['caller_instructions'], done_when=spec['caller_done_when'],
                      model=spec['model'], model_reason=spec['model_reason'], task_type=spec['task_type'],
                      workflow=spec['workflow']['name'], workflow_stage=spec['stage'], workflow_inputs=spec['workflow']['inputs'],
                      verifies=spec.get('verifies'), base_branch=spec.get('base_branch'), backend=spec.get('backend'),
                      _run_attempt=_Reservation(attempt['id']))
        # A fast worker may already have reported; never overwrite its result state.
        con.execute("UPDATE workflow_run_attempts SET state='running' WHERE id=? AND state='launching'", (attempt['id'],))
    except Exception as error:
        _state(con, run['id'], 'needs_recovery', 'Launch did not return a confirmed worker; inspect reserved task before recovery')
        _event(con, run['id'], 'launch_uncertain', dict(error_type=type(error).__name__), attempt['task_id'])
        if run['parent_run_id']:
            _state(con, run['parent_run_id'], 'needs_recovery', 'Recovery worker launch is uncertain; no duplicate worker was spawned')


def _success(con, run, author, reviewer=None):
    with store.immediate(con):
        con.execute("UPDATE workflow_run_attempts SET state='succeeded' WHERE id=?", (author['id'],))
        if reviewer:
            con.execute("UPDATE workflow_run_attempts SET state='succeeded' WHERE id=?", (reviewer['id'],))
        con.execute('UPDATE workflow_runs SET step_index=step_index+1,version=version+1,updated_at=? WHERE id=?', (time.time(), run['id']))
        _event(con, run['id'], 'step_succeeded', dict(step_index=run['step_index'], basis='native_verification_and_report' if reviewer else 'task_self_report'), author['task_id'])


def migrate_owner(con, old_orch, new_orch):
    """Restart transaction hook. Frozen request destinations remain unchanged."""
    rows = con.execute("SELECT id FROM workflow_runs WHERE orch_thread=? AND state<>'done'", (old_orch,)).fetchall()
    for row in rows:
        con.execute('UPDATE workflow_runs SET orch_thread=?,version=version+1,updated_at=? WHERE id=?', (new_orch, time.time(), row['id']))
        con.execute("UPDATE tasks SET orch_thread=? WHERE orch_thread=? AND status<>'closed' AND id IN (SELECT task_id FROM workflow_run_attempts WHERE run_id=?)", (new_orch, old_orch, row['id']))
        _event(con, row['id'], 'owner_migrated', dict(old_orch=old_orch, new_orch=new_orch))
    return len(rows)


def _step(con, rt, run):
    from . import verify
    run = _get(con, run['id'])
    if run['state'] != 'active':
        return
    plan = _json(run['plan'])
    origin = json.loads(run['origin'])
    request = con.execute('SELECT * FROM interface_requests WHERE id=?', (run['request_id'],)).fetchone()
    if request is None or any(request[key] != origin[key] for key in ('interface_id', 'conversation', 'request_key')) or request['dismissed_at'] is not None:
        raise ValueError('Workflow reply origin changed or was dismissed')
    if request['orch_id'] != run['orch_thread']:
        with store.immediate(con):
            migrate_owner(con, run['orch_thread'], request['orch_id'])
        run = _get(con, run['id'])
    for name, expected in origin['repositories'].items():
        if _repo(rt, name) != expected:
            raise ValueError('Registered repository path changed since run start')
    # A verified author moving after acceptance invalidates later transitions too.
    for previous in con.execute("SELECT * FROM workflow_run_attempts WHERE run_id=? AND role='author' AND state='succeeded'", (run['id'],)):
        if _json(previous['spec']).get('verification') and verify.status(con, previous['task_id'])['state'] != 'pass':
            _blocked(con, run, 'Previously accepted verification is stale or failed')
            return
    if run['step_index'] == len(plan['steps']):
        from . import core, interface
        for attempt in con.execute('SELECT task_id FROM workflow_run_attempts WHERE run_id=?', (run['id'],)).fetchall():
            try:
                core.close(con, rt, attempt['task_id'], outcome='done')
            except Exception as error:
                _event(con, run['id'], 'cleanup_pending', dict(error_type=type(error).__name__), attempt['task_id'])
                return
        with _transition_lock(con):
            if _get(con, run['id'])['state'] != 'active':
                return
            if run['parent_run_id']:
                final = 'Workflow run ' + run['parent_run_id'] + ' produced a bounded recovery candidate in ' + run['id'] + '. It needs adoption; no merge, deployment or active pin change was performed.'
            else:
                final = 'Workflow run ' + run['id'] + ' finished its declared steps. Reports are self-reported; native verification is recorded where required.'
            interface.reply(con, run['orch_thread'], request_id=run['request_id'], text=final)
            _state(con, run['id'], 'needs_adoption' if run['parent_run_id'] else 'done')
            if run['parent_run_id']:
                _state(con, run['parent_run_id'], 'needs_adoption', 'Bounded recovery candidate awaits adoption')
                for original in con.execute('SELECT task_id FROM workflow_run_attempts WHERE run_id=?', (run['parent_run_id'],)).fetchall():
                    try:
                        core.close(con, rt, original['task_id'], outcome='parked')
                    except Exception as error:
                        _event(con, run['parent_run_id'], 'cleanup_pending', dict(error_type=type(error).__name__), original['task_id'])
            _event(con, run['id'], 'finished', dict(request_id=run['request_id']))
        return
    node = plan['steps'][run['step_index']]
    attempts = {r['role']: r for r in con.execute('SELECT * FROM workflow_run_attempts WHERE run_id=? AND step_index=?', (run['id'], run['step_index']))}
    author = attempts.get('author')
    if author is None:
        with store.immediate(con):
            author = _reserve(con, run, node, 'author')
    if author['state'] == 'reserved':
        _launch(con, rt, run, author)
        return
    if author['state'] == 'launching':
        _state(con, run['id'], 'needs_recovery', 'Interrupted launch; do not spawn a second worker')
        return
    if author['state'] != 'reported':
        return
    if 'verification' not in node:
        _success(con, run, author)
        return
    reviewer = attempts.get('reviewer')
    if reviewer is None:
        locked = verify.lock(con, author['task_id'], node['verification']['paths'], node['verification']['command'], run['orch_thread'])
        with store.immediate(con):
            con.execute('UPDATE workflow_run_attempts SET lock_id=? WHERE id=?', (locked['lock_id'], author['id']))
            reviewer = _reserve(con, run, node['verification'], 'reviewer', author)
    if reviewer['state'] == 'reserved':
        _launch(con, rt, run, reviewer)
        return
    if reviewer['state'] == 'launching':
        _state(con, run['id'], 'needs_recovery', 'Interrupted verifier launch; inspect before recovery')
        return
    status = verify.status(con, author['task_id'])
    if status['state'] in ('fail', 'stale'):
        latest_result = con.execute("SELECT id FROM messages WHERE task_id=? AND kind=? ORDER BY id DESC LIMIT 1", (author['task_id'], verify.RESULT)).fetchone()
        _fail(con, run, 'Native verification ' + status['state'], task_id=author['task_id'], message_id=latest_result['id'] if latest_result else None, verification=status)
    elif reviewer['state'] == 'reported' and status['state'] == 'pass' and status.get('verifier') == reviewer['task_id']:
        latest = verify.latest(con, author['task_id'], verify.LOCK)
        if latest and latest['id'] == author['lock_id']:
            _success(con, run, author, reviewer)


def _database(con):
    value = con.execute('PRAGMA database_list').fetchone()['file']
    if not value:
        raise ValueError('Workflow runner needs a persistent task database')
    return Path(value)


@contextlib.contextmanager
def _locked(con):
    file = _database(con).with_suffix('.workflow-runner.lock')
    with file.open('a+b') as handle:
        try_lock(handle)
        yield


def reconcile(con, rt, run_id=None, *, limit=100):
    """Apply durable transitions. A single profile lock fences launch effects across processes."""
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError('Invalid reconcile limit')
    try:
        with _locked(con):
            runs = [_get(con, run_id)] if run_id else con.execute("SELECT * FROM workflow_runs WHERE state IN ('active','needs_recovery','blocked','awaiting_input','repairing','cancelling') ORDER BY created_at LIMIT ?", (limit,)).fetchall()
            for run in runs:
                if run['state'] == 'cancelling':
                    try:
                        _cancel_cleanup(con, rt, run)
                    except Exception as error:
                        if not con.execute("SELECT 1 FROM workflow_run_events WHERE run_id=? AND kind='cancel_cleanup_pending' AND task_id IS NULL", (run['id'],)).fetchone():
                            _event(con, run['id'], 'cancel_cleanup_pending', dict(error_type=type(error).__name__))
                    continue
                if run['state'] != 'active':
                    _notify_attention(con, run)
                    continue
                deadline = _json(run['plan']).get('limits', {}).get('deadline_seconds')
                if deadline and time.time() > run['created_at'] + deadline:
                    _blocked(con, run, 'Workflow deadline exceeded')
                    _notify_attention(con, _get(con, run['id']))
                    continue
                try:
                    _step(con, rt, run)
                except Exception as error:
                    _blocked(con, run, 'Reconcile refused: ' + type(error).__name__)
                    _event(con, run['id'], 'reconcile_error', dict(error_type=type(error).__name__))
                _notify_attention(con, _get(con, run['id']))
            return dict(runs=[show(con, r['id']) for r in runs])
    except BlockingIOError:
        return dict(runs=[], busy=True)


def serve(database, rt, *, interval=1.0, once=False):
    """One profile-local loop owner; reconcile has its own short-lived effect fence."""
    if not isinstance(interval, (int, float)) or not 0.05 <= interval <= 60:
        raise ValueError('Invalid workflow runner interval')
    con = store.connect(database)
    try:
        with _database(con).with_suffix('.workflow-service.lock').open('a+b') as handle:
            try:
                try_lock(handle)
            except BlockingIOError:
                return dict(runs=[], busy=True)
            while True:
                result = reconcile(con, rt)
                if once:
                    return result
                time.sleep(interval)
    finally:
        con.close()


def ensure_runner(con, rt):
    """Start the installed core loop, never a plugin script. Memory databases cannot spawn."""
    import subprocess
    from .platform_process import background_python, detached_options
    try:
        database = _database(con)
    except ValueError:
        return dict(state='skipped', reason='nonpersistent database')
    if not con.execute("SELECT 1 FROM workflow_runs WHERE state IN ('active','needs_recovery','blocked','awaiting_input','repairing','cancelling') LIMIT 1").fetchone():
        return dict(state='idle')
    service_lock = database.with_suffix('.workflow-service.lock')
    with service_lock.open('a+b') as handle:
        try:
            try_lock(handle)
        except BlockingIOError:
            return dict(state='running')
    # Concurrent starters may create short-lived children, but serve owns a single lifetime lock.
    env = dict(os.environ)
    env['PYTHONPATH'] = str(Path(__file__).resolve().parents[1]) + os.pathsep + env.get('PYTHONPATH', '')
    command = [background_python(), '-P', '-m', 'orchd.cli', 'workflow', 'run', 'serve', '--database', str(database)]
    with database.with_suffix('.workflow-runner.log').open('ab') as log:
        child = subprocess.Popen(command, cwd=database.parent, env=env, stdin=subprocess.DEVNULL,
                                 stdout=log, stderr=log, **detached_options())
    return dict(state='starting', pid=child.pid)


@contextlib.contextmanager
def _transition_lock(con):
    """Short reply/cancel fence, separate from worktree/spawn effects."""
    with _database(con).with_suffix('.workflow-transition.lock').open('a+b') as handle:
        deadline = time.monotonic() + 5
        while True:
            try:
                try_lock(handle)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError('Workflow transition busy; retry cancellation')
                time.sleep(.02)
        yield


def _family(con, run):
    root = _get(con, run['parent_run_id']) if run['parent_run_id'] else run
    rows = con.execute('SELECT * FROM workflow_runs WHERE id=? OR parent_run_id=? ORDER BY created_at', (root['id'], root['id'])).fetchall()
    return root, rows


def _cancel_cleanup(con, rt, run):
    from . import core, interface
    root, family = _family(con, run)
    if root['state'] != 'cancelling':
        return
    for member in family:
        for attempt in con.execute('SELECT task_id FROM workflow_run_attempts WHERE run_id=?', (member['id'],)).fetchall():
            if not con.execute('SELECT 1 FROM tasks WHERE id=?', (attempt['task_id'],)).fetchone():
                continue  # Reservation never became a task; no spawn is inferred.
            try:
                core.close(con, rt, attempt['task_id'], outcome='parked')
            except Exception as error:
                if not con.execute("SELECT 1 FROM workflow_run_events WHERE run_id=? AND kind='cancel_cleanup_pending' AND task_id=?", (root['id'], attempt['task_id'])).fetchone():
                    _event(con, root['id'], 'cancel_cleanup_pending', dict(error_type=type(error).__name__), attempt['task_id'])
                return
    with _transition_lock(con):
        request = con.execute('SELECT * FROM interface_requests WHERE id=?', (root['request_id'],)).fetchone()
        origin = json.loads(root['origin'])
        if request is None or any(request[k] != origin[k] for k in ('interface_id', 'conversation', 'request_key')):
            raise ValueError('Cancellation reply origin no longer matches its frozen request')
        if request['reply'] is None:
            interface.reply(con, request['orch_id'], request_id=root['request_id'],
                            text='Workflow run ' + root['id'] + ' was cancelled; it did not finish successfully. ' + (root['note'] or ''))
            notification = 'cancelled_final_recorded'
        else:
            notification = 'existing_final_retained'
        with store.immediate(con):
            for member in family:
                _state(con, member['id'], 'cancelled', root['note'])
            _event(con, root['id'], 'cancelled', dict(run_ids=[r['id'] for r in family], origin_notification=notification))


def cancel(con, rt, run_id, *, orch_thread, reason):
    """Cancel one operation and its sole recovery child; never close unrelated tasks."""
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 4096:
        raise ValueError('Cancellation needs a nonempty reason under 4096 characters')
    with _transition_lock(con):
        run = _get(con, run_id)
        root, family = _family(con, run)
        request = con.execute('SELECT orch_id FROM interface_requests WHERE id=?', (root['request_id'],)).fetchone()
        if request is None or request['orch_id'] != orch_thread:
            raise ValueError('Cancellation request is not owned by this Orch')
        if root['state'] in ('done', 'needs_adoption'):
            live = any(con.execute("SELECT 1 FROM tasks t JOIN workflow_run_attempts a ON a.task_id=t.id WHERE a.run_id=? AND t.status<>'closed' LIMIT 1", (member['id'],)).fetchone() for member in family)
            if not live:
                return dict(show(con, root['id']), cancelled=False, already_terminal=True)
        if root['state'] not in ('cancelling', 'cancelled'):
            with store.immediate(con):
                for member in family:
                    _state(con, member['id'], 'cancelling', reason)
                _event(con, root['id'], 'cancellation_requested', dict(reason=reason, orch_thread=orch_thread, run_ids=[r['id'] for r in family]))
    if _get(con, root['id'])['state'] == 'cancelling':
        try:
            with _locked(con):
                _cancel_cleanup(con, rt, _get(con, root['id']))
        except BlockingIOError:
            pass  # Intent is durable. The existing runner will finish scoped cleanup.
    result = show(con, root['id'])
    event = con.execute("SELECT body FROM workflow_run_events WHERE run_id=? AND kind='cancelled' ORDER BY id DESC LIMIT 1", (root['id'],)).fetchone()
    return dict(result, cancelled=result['state'] == 'cancelled', cleanup_pending=result['state'] == 'cancelling',
                origin_notification=json.loads(event['body'])['origin_notification'] if event else 'pending')
