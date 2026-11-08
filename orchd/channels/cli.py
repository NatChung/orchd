"""Local CLI facade. Platform transport is not available in PR2."""
import json
import os
import sqlite3
import sys
from orchd import store
from . import policy, repository, service


def run(args):
    svc = None
    try:
        svc=service.ChannelService(store.home() / 'orchd.db', retention=policy.retention_policies())
        identity=os.environ.get('ORCHD_ORCH_ID') or os.environ.get('CODEX_THREAD_ID')
        if os.environ.get('ORCHD_WORKTREE'):
            caller=policy.caller_for_worker(svc.con,os.environ['ORCHD_WORKTREE'])
        elif identity:
            caller=policy.caller_for_identity(identity)
        else:
            caller=policy.Caller(role='operator',sources=repository.source_ids(svc.con),identity='operator')
        action=args.channel_action
        if action == 'messages':
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
