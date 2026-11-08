"""CLI boundary for persisted workflow runs; workers report artifacts instead."""

import json
import os
import sqlite3
import sys
from pathlib import Path

from . import store, workflows
from .runtime import Runtime


def add_parser(actions, context_parser):
    parser = actions.add_parser('run', help='start and inspect a fixed workflow run')
    commands = parser.add_subparsers(dest='run_action', required=True)
    start = commands.add_parser('start')
    source = start.add_mutually_exclusive_group(required=True)
    source.add_argument('--plan-file', type=Path, help='Operator-only explicit plan')
    source.add_argument('--workflow', help='installed workflow owning the committed run template')
    start.add_argument('--template')
    start.add_argument('--context', type=context_parser, default={})
    start.add_argument('--request-id', type=int, required=True)
    start.add_argument('--idempotency-key', required=True)
    start.add_argument('--orch-id')
    show = commands.add_parser('show')
    show.add_argument('run_id')
    cancel = commands.add_parser('cancel', help='stop this run family and retain its evidence')
    cancel.add_argument('run_id')
    cancel.add_argument('--reason', required=True)
    cancel.add_argument('--orch-id')
    export = commands.add_parser('export', help='export only this run task\'s audit evidence')
    export.add_argument('--task-id', required=True)
    export.add_argument('--output', type=Path, required=True)
    reconcile = commands.add_parser('reconcile')
    reconcile.add_argument('run_id', nargs='?')
    reconcile.add_argument('--limit', type=int, default=100)
    serve = commands.add_parser('serve')
    serve.add_argument('--database', type=Path, required=True)
    serve.add_argument('--interval', type=float, default=1.0)
    serve.add_argument('--once', action='store_true')


def run(args, resolve_orch):
    from . import workflow_runs

    con = None
    try:
        if os.environ.get('ORCHD_WORKTREE') and args.run_action != 'export':
            raise ValueError('Workers cannot start or control workflow runs; report their declared output artifact')
        rt = Runtime()
        if args.run_action == 'serve':
            if args.interval <= 0:
                raise ValueError('Workflow runner interval must be positive')
            workflow_runs.serve(args.database, rt, interval=args.interval, once=args.once)
            return 0
        con = store.connect()
        if args.run_action == 'export':
            from .workflow_run_export import export_for_task
            result = export_for_task(con, args.task_id, args.output)
        elif args.run_action == 'start':
            if getattr(args, 'workflow', None):
                if not args.template:
                    raise ValueError('A workflow start needs its installed template name')
                plan = workflows.run_template(args.workflow, args.template, args.context)
            else:
                if os.environ.get('ORCHD_ORCH_ID'):
                    raise ValueError('Orch starts a committed workflow template; explicit plan files are Operator-only')
                if getattr(args, 'template', None) or getattr(args, 'context', {}):
                    raise ValueError('Template/context overrides require --workflow')
                plan = workflows.load(args.plan_file.read_bytes())
            result = workflow_runs.start(
                con, rt, plan=plan, request_id=args.request_id,
                orch_thread=resolve_orch(con, args.orch_id), idempotency_key=args.idempotency_key)
            workflow_runs.ensure_runner(con, rt)
        elif args.run_action == 'show':
            result = workflow_runs.show(con, args.run_id)
        elif args.run_action == 'cancel':
            result = workflow_runs.cancel(con, rt, args.run_id,
                                          orch_thread=resolve_orch(con, args.orch_id), reason=args.reason)
            if result.get('state') == 'cancelling':
                workflow_runs.ensure_runner(con, rt)
        else:
            if args.limit < 1:
                raise ValueError('Workflow reconcile limit must be positive')
            result = workflow_runs.reconcile(con, rt, args.run_id, limit=args.limit)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, KeyError, TypeError, OSError, RuntimeError, sqlite3.Error) as error:
        print(f'orchd workflow run: {error}', file=sys.stderr)
        return 1
    finally:
        if con is not None:
            con.close()
