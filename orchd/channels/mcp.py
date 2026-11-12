"""MCP channel calls bind the caller to the server-side thread and local ACL."""
from . import policy, service


def call(name, args, thread, con):
    allowed = {
        'channels_messages': ({'source_id'}, {'source_id','limit','cursor'}),
        'channels_search': ({'source_id','query'}, {'source_id','query','limit'}),
        'channels_message': ({'message_id'}, {'message_id'}),
        'channels_conversation': ({'conversation_id'}, {'conversation_id','cursor','limit'}),
        'channels_sync': ({'source_id','request_key'}, {'source_id','request_key'}),
        'channels_sync_status': ({'job_id'}, {'job_id'}),
        'channels_attachment_request': ({'attachment_id'}, {'attachment_id'}),
        'channels_attachment_media': ({'attachment_id'}, {'attachment_id'}),
        'channels_attachment_read': ({'attachment_id'}, {'attachment_id'}),
        'channels_approve': ({'draft_id','version','payload_hash'}, {'draft_id','version','payload_hash'}),
        'channels_draft': ({'message_id','payload'}, {'message_id','payload'}),
    }
    required, accepted=allowed.get(name,(None,None))
    if required is None or not isinstance(args,dict) or not required <= set(args) or set(args) - accepted:
        raise ValueError('invalid channel arguments')
    caller=policy.caller_for_identity(thread)
    path=con.execute('PRAGMA database_list').fetchone()[2]
    if not path:
        raise ValueError('channel API requires a persistent database')
    svc=service.ChannelService(path, retention=policy.retention_policies())
    try:
        if name == 'channels_messages':
            return svc.messages(args['source_id'],caller=caller,limit=args.get('limit',100),cursor=args.get('cursor',''))
        if name == 'channels_search':
            return svc.search(args['source_id'],args['query'],caller=caller,limit=args.get('limit',100))
        if name == 'channels_message':
            return svc.message(args['message_id'],caller=caller)
        if name == 'channels_conversation':
            return svc.conversation(args['conversation_id'],caller=caller,cursor=args.get('cursor',''),limit=args.get('limit',100))
        if name == 'channels_sync':
            return svc.request_sync(args['source_id'],args['request_key'],{},caller=caller)
        if name == 'channels_sync_status':
            return svc.sync_status(args['job_id'],caller=caller)
        if name == 'channels_attachment_request':
            return svc.request_attachment(args['attachment_id'],caller=caller)
        if name == 'channels_approve':
            return svc.approve(**args,caller=caller)
        if name == 'channels_draft':
            return svc.draft_reply(args['message_id'],args['payload'],caller=caller)
        if name == 'channels_attachment_media':
            return svc.attachment_media(args['attachment_id'],caller=caller)
        return svc.read_attachment(args['attachment_id'],caller=caller)
    finally:
        svc.close()
