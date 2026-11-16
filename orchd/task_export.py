"""Export a selected task and request without claiming an ordered run or semantic acceptance."""
import difflib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys

from . import store, workflows
from .workflow_run_export import _destination, _digest


KINDS = ('dispatch', 'ack', 'progress', 'question', 'answer', 'report', 'close',
         'app_event', 'verify_lock', 'verification')


def export_task(con, task_id, output_dir, *, request_id=None, input_key=None, env=None):
    """Export one task; an explicit request selector is evidence selection, not a stored origin binding."""
    if not isinstance(task_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', task_id):
        raise ValueError('Invalid task export ID')
    if request_id is not None and (type(request_id) is not int or request_id < 1):
        raise ValueError('Request ID must be a positive integer')
    env = os.environ if env is None else env
    con.execute('SAVEPOINT task_evidence_export')
    try:
        row = con.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
        if row is None:
            raise ValueError('Task is unavailable')
        task = dict(row)
        if env.get('ORCHD_ORCH_ID') and env['ORCHD_ORCH_ID'] != task['orch_thread']:
            raise ValueError('Task export belongs to another Orch')
        output = _destination(task, output_dir, env)
        messages = [dict(row) for row in con.execute(
            'SELECT id,task_id,kind,body,evidence,created_at FROM messages '
            'WHERE task_id=? AND kind IN (' + ','.join('?' for _ in KINDS) + ') ORDER BY id',
            (task_id, *KINDS))]
        request = None
        if request_id is not None:
            row = con.execute('SELECT id,interface_id,orch_id,conversation,request_key,body,created_at '
                              'FROM interface_requests WHERE id=?', (request_id,)).fetchone()
            if row is None:
                raise ValueError('Selected request is unavailable')
            request = dict(row)
            if request['orch_id'] != task['orch_thread']:
                raise ValueError('Selected request and task belong to different Orchs')
    finally:
        con.execute('RELEASE SAVEPOINT task_evidence_export')

    dispatches = [row for row in messages if row['kind'] == 'dispatch']
    workflow = None
    reason = None
    if len(dispatches) != 1:
        reason = 'missing_or_ambiguous_dispatch'
    else:
        try:
            record = json.loads(dispatches[0]['body'])
            workflow = record.get('workflow') if isinstance(record, dict) else None
        except (ValueError, TypeError):
            reason = 'invalid_dispatch_metadata'
    inputs = workflow.get('inputs') if isinstance(workflow, dict) else None
    selected = input_key
    if selected is None and isinstance(inputs, dict) and len(inputs) == 1:
        selected = next(iter(inputs))
    value = inputs.get(selected) if isinstance(inputs, dict) and selected is not None else None
    if request is None:
        reason = reason or 'request_not_selected'
    elif not isinstance(value, str):
        reason = reason or 'missing_or_ambiguous_workflow_input'
    original = request['body'].encode('utf-8') if request is not None else None
    supplied = value.encode('utf-8') if isinstance(value, str) else None
    comparison = dict(status='missing' if reason else 'identical' if original == supplied else 'different',
                      reason=reason, input_key=selected,
                      request_digest=_digest(original) if original is not None else None,
                      dispatch_input_digest=_digest(supplied) if supplied is not None else None,
                      request_in_worker_instructions=(request['body'] in task['instructions'])
                      if request is not None else None,
                      basis='Exact UTF-8 bytes only; differences are not a semantic verdict.')
    files = {
        'task.json': workflows.canonical({key: task.get(key) for key in (
            'id', 'repo', 'title', 'instructions', 'done_when', 'orch_thread', 'base', 'base_branch',
            'branch', 'worktree', 'session_id', 'status', 'created_at', 'updated_at')}),
        'workflow.json': workflows.canonical(workflow),
        'core.jsonl': b''.join(workflows.canonical(row) for row in messages),
        'comparison.json': workflows.canonical(comparison),
    }
    if request is not None:
        files['request.json'] = workflows.canonical(request)
        files['request.txt'] = original
    if supplied is not None:
        files['dispatch-input.txt'] = supplied
    if original is not None and supplied is not None:
        files['request-to-dispatch.diff'] = ''.join(difflib.unified_diff(
            request['body'].splitlines(keepends=True), value.splitlines(keepends=True),
            fromfile='request.txt', tofile='dispatch-input.txt')).encode('utf-8')
    receipt = dict(schema_version='orchd-task-export/v1', task_id=task_id,
                   request_id=request_id, request_selection='explicit_same_orch_not_origin_attestation',
                   output_dir=str(output), comparison=comparison,
                   message_count=len(messages), last_message_id=messages[-1]['id'] if messages else None,
                   files={name: _digest(raw) for name, raw in files.items()},
                   limitations=[
                       'The caller selects the request ID; matching Orch ownership does not prove task/request origin.',
                       'No ordered run is inferred. Missing evidence stays missing.',
                       'Only this task and the explicitly selected request are exported; no other chats or credentials.',
                       'Core app events contain item metadata, not worker command output or hidden reasoning.',
                       'Local DB records and receipts are not signed proof of authenticity or workflow compliance.'])
    output.mkdir(mode=0o700)
    for name, raw in {**files, 'receipt.json': workflows.canonical(receipt)}.items():
        with (output / name).open('xb') as stream:
            stream.write(raw)
        (output / name).chmod(0o600)
    return receipt


def add_parser(actions):
    parser = actions.add_parser('export', help='export one task and compare an explicitly selected request')
    parser.add_argument('--task-id', required=True)
    parser.add_argument('--request-id', type=int)
    parser.add_argument('--input-key', help='workflow input to compare; optional only for a single input')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--check', action='store_true', help='exit 1 for different bytes, 2 for missing comparison evidence')


def run(args):
    con = None
    try:
        con = store.connect_readonly()
        result = export_task(con, args.task_id, args.output, request_id=args.request_id,
                             input_key=args.input_key)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.check:
            return {'identical': 0, 'different': 1, 'missing': 2}[result['comparison']['status']]
        return 0
    except (ValueError, OSError, KeyError, TypeError, RuntimeError, sqlite3.Error) as error:
        print(f'orchd task export: {error}', file=sys.stderr)
        return 1
    finally:
        if con is not None:
            con.close()
