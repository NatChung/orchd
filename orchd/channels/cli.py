"""Local CLI facade. Approval/reconciliation are control-plane operations."""
import json
import os
import sqlite3
import sys
from orchd import store
from . import policy, repository, service


def run(args):
    svc = None
    try:
        if args.channel_action in ('status', 'doctor'):
            from .health import status
            print(json.dumps(status(), ensure_ascii=False))
            return 0
        svc=service.ChannelService(store.home() / 'orchd.db', retention=policy.retention_policies())
        identity=os.environ.get('ORCHD_ORCH_ID') or os.environ.get('CODEX_THREAD_ID')
        if os.environ.get('ORCHD_WORKTREE'):
            caller=policy.caller_for_worker(svc.con,os.environ['ORCHD_WORKTREE'])
        elif identity:
            caller=policy.caller_for_identity(identity)
        else:
            caller=policy.Caller(role='operator',sources=repository.source_ids(svc.con),identity='operator')
        action=args.channel_action
        if action == 'cutover':
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
    except (ValueError, PermissionError, sqlite3.Error, OSError) as error:
        print(json.dumps({'status':'failed','error':str(error)},ensure_ascii=False),file=sys.stderr)
        return 1
    finally:
        if svc is not None:
            svc.close()
