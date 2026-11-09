"""Local CLI facade. Approval/reconciliation are control-plane operations."""
import json
import os
import sqlite3
import sys
from orchd import store
from . import policy, repository, service


def put_secret(adapter, source, reference, value, *, provider=None):
    from .secret_store import default_store
    (provider or default_store()).put(adapter, source, reference, value)


def provision_source(svc, caller, payload):
    svc.provision_source(payload, caller=caller)
    return {'status': 'provisioned', 'id': payload['id']}


def run(args):
    svc = None
    try:
        if args.channel_action == 'setup' and args.dry_run:
            from . import setup
            setup.run(dry_run=True)
            return 0
        if args.channel_action in ('status', 'doctor'):
            from .health import status
            print(json.dumps(status(), ensure_ascii=False))
            return 0
        from pathlib import Path
        db_home = Path(args.service_store_home) if args.channel_action == 'serve' and args.service_store_home else store.home()
        channel_home = Path(args.service_channel_home) if args.channel_action == 'serve' and args.service_channel_home else None
        svc=service.ChannelService(db_home / 'orchd.db', retention=policy.retention_policies(), channel_home=channel_home)
        identity=os.environ.get('ORCHD_ORCH_ID') or os.environ.get('CODEX_THREAD_ID')
        if os.environ.get('ORCHD_WORKTREE'):
            caller=policy.caller_for_worker(svc.con,os.environ['ORCHD_WORKTREE'])
        elif identity:
            caller=policy.caller_for_identity(identity)
        else:
            caller=policy.Caller(role='operator',sources=repository.source_ids(svc.con),identity='operator')
        action=args.channel_action
        if action == 'setup':
            policy.require_operator(caller)
            from . import setup, management
            def execute(operation, payload):
                if operation == 'check':
                    if any(row['id'] == payload['source']['id'] for row in management.sources(svc, caller)):
                        raise ValueError('source already exists')
                    if any(row['id'] == payload['route']['id'] for row in svc.routes(caller=caller)):
                        raise ValueError('route already exists')
                elif operation == 'secret':
                    adapter, source_id, ref, value = payload
                    put_secret(adapter, source_id, ref, value)
                elif operation == 'source':
                    provision_source(svc, caller, payload)
                elif operation == 'route':
                    management.change_route(svc, caller, 'add', payload)
                elif operation == 'enable':
                    management.set_enabled(svc, caller, payload, True)
                elif operation == 'doctor':
                    from .health import status
                    print(json.dumps(status(), ensure_ascii=False))
                elif operation == 'service':
                    from . import system_service
                    system_service.command(payload, svc.channel_home, con=svc.con)
            setup.run(dry_run=args.dry_run, execute=execute)
            return 0
        if action == 'secret':
            policy.require_operator(caller)
            import getpass
            from .secret_store import default_store
            provider = default_store()
            if args.secret_action == 'list':
                result = provider.list(args.adapter, args.source)
            else:
                if not args.ref: raise ValueError('secret ref required')
                if args.secret_action == 'put':
                    value = getpass.getpass('Secret: ').encode() if sys.stdin.isatty() else sys.stdin.buffer.read()
                    put_secret(args.adapter, args.source, args.ref, value, provider=provider)
                else:
                    provider.delete(args.adapter, args.source, args.ref)
                result = {'status': 'ok', 'ref': args.ref}
            print(json.dumps(result))
            return 0
        if action == 'service':
            policy.require_operator(caller)
            from . import system_service
            print(json.dumps(system_service.command(args.service_action, svc.channel_home, con=svc.con)))
            return 0
        if action == 'serve':
            policy.require_operator(caller)
            from .serve import run as serve
            if args.service_log:
                import contextlib
                from pathlib import Path
                home = Path(svc.channel_home)
                home.mkdir(parents=True, exist_ok=True)
                with (home / 'serve.log').open('a', encoding='utf-8', buffering=1) as log, contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
                    try: serve(svc, host=args.host, port=args.port)
                    except Exception as error:
                        print('channel serve failed:', type(error).__name__)
                        raise
            else:
                serve(svc, host=args.host, port=args.port)
            return 0
        if action in ('source', 'route'):
            from . import management
            policy.require_operator(caller)
            subaction = getattr(args, action + '_action')
            if subaction == 'list':
                result = management.sources(svc, caller) if action == 'source' else svc.routes(caller=caller)
            elif subaction in ('enable', 'disable'):
                if not args.id: raise ValueError('source id required')
                result = management.set_enabled(svc, caller, args.id, subaction == 'enable')
            elif subaction == 'remove':
                if not args.id: raise ValueError('route id required')
                result = management.change_route(svc, caller, 'remove', args.id)
            else:
                if args.file == '-': payload = json.load(sys.stdin)
                else:
                    with open(args.file, encoding='utf-8') as handle: payload = json.load(handle)
                if action == 'source':
                    result = provision_source(svc, caller, payload)
                else: result = management.change_route(svc, caller, 'add', payload)
        elif action == 'cutover':
            result = svc.cutover(args.source, args.mode, backfill=args.backfill_digest, caller=caller)
        elif action == 'ledger':
            result = svc.cutover_ledger(args.source, after=args.after, through=args.through, caller=caller)
        elif action == 'messages':
            result=svc.messages(args.source,caller=caller,limit=args.limit,cursor=args.cursor)
        elif action == 'search':
            result=svc.search(args.source,args.query,caller=caller,limit=args.limit)
        elif action == 'message':
            result=svc.message(args.id,caller=caller)
        elif action == 'conversation':
            result=svc.conversation(args.id,caller=caller,cursor=args.cursor,limit=args.limit)
        elif action == 'sync':
            result=svc.request_sync(args.source,args.request_key,{},caller=caller)
        elif action == 'sync-status':
            result=svc.sync_status(args.id,caller=caller)
        elif action == 'draft':
            if args.file == '-':
                payload=json.load(sys.stdin)
            else:
                with open(args.file, encoding='utf-8') as handle:
                    payload=json.load(handle)
            if not isinstance(payload, dict) or set(payload) != {'message_id', 'payload'}:
                raise ValueError('draft expects message_id and payload')
            result=svc.draft_reply(payload['message_id'],payload['payload'],caller=caller)
        elif action in ('approve', 'reconcile'):
            policy.require_operator(caller)
            if args.file == '-':
                payload = json.load(sys.stdin)
            else:
                with open(args.file, encoding='utf-8') as handle:
                    payload = json.load(handle)
            required = ({'draft_id','version','payload_hash','entry_answer_ref'} if action == 'approve'
                        else {'delivery_id','outcome','provider_receipt','evidence'})
            if not isinstance(payload, dict) or set(payload) != required:
                raise ValueError('invalid '+action+' fields')
            result = (svc.approve if action == 'approve' else svc.reconcile_outbound)(**payload, caller=caller)
        elif action == 'approval-preview':
            result = svc.approval_preview(args.draft_id, args.version, caller=caller)
        elif action == 'delivery':
            result = svc.delivery(args.id, caller=caller)
        elif action == 'recover-outbound':
            result = svc.recover_outbound(caller=caller)
        elif action == 'attachment':
            result=(svc.request_attachment if args.action == 'request' else svc.read_attachment)(args.id,caller=caller)
        elif action == 'retention':
            result=svc.enforce_retention(caller=caller)
        else:
            raise ValueError('unknown channel command')
        print(json.dumps(result,ensure_ascii=False))
        return 0
    except (ValueError, PermissionError, sqlite3.Error, OSError, RuntimeError) as error:
        print(json.dumps({'status':'failed','error':str(error)},ensure_ascii=False),file=sys.stderr)
        return 1
    finally:
        if svc is not None:
            svc.close()
