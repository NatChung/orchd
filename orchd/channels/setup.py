"""Interactive setup. IO and command execution can be supplied by callers."""
import getpass
import json
import re
import sys

SLACK_MANIFEST = '''{
  "display_information": {"name": "orchd"},
  "features": {"bot_user": {"display_name": "orchd"}},
  "oauth_config": {"scopes": {"bot": ["channels:history", "groups:history", "im:history", "mpim:history", "files:read"]}},
  "settings": {"event_subscriptions": {"bot_events": ["message.channels", "message.groups", "message.im", "message.mpim"]}, "socket_mode_enabled": true}
}'''


def run(*, input_fn=input, secret_fn=getpass.getpass, output=None, dry_run=False, execute=None):
    output = output or sys.stdout
    platform = input_fn('Platform (slack/line): ').strip().lower()
    if platform not in ('slack', 'line'):
        raise ValueError('platform must be slack or line')
    if platform == 'slack':
        print('Create a Slack app with this manifest, enable Socket Mode, create an app token with connections:write, install the app, and invite it to the conversation:', SLACK_MANIFEST, file=output)
    else:
        print('Create a LINE Messaging API channel and arrange a public HTTPS endpoint for its webhook.', file=output)
    sid = input_fn('Source ID: ').strip()
    tenant = input_fn('Workspace/team ID (LINE: channel ID): ').strip()
    conversation = input_fn('Conversation ID: ').strip()
    owner = input_fn('Owner Orch ID: ').strip()
    if not all((sid, tenant, conversation, owner)):
        raise ValueError('all fields are required')
    if not re.fullmatch(r'[A-Za-z0-9_-]+', sid):
        raise ValueError('invalid source id')
    names = ('app', 'api') if platform == 'slack' else ('signing',)
    refs = {name: f'os-store:{sid}/{name}' for name in names}
    source = dict(id=sid, host_id='local', provider=platform, tenant_ref=tenant,
                  account_ref=tenant, adapter_id=platform, credential_refs=refs, enabled=False)
    route = dict(id=sid + '-route', source_id=sid, conversation_ref=conversation, owner_orch=owner)
    result = dict(source=source, route=route)
    if dry_run:
        result['install_service'] = input_fn('Install service? (y/N): ').strip().lower() == 'y'
        print('dry-run: ' + json.dumps(result), file=output)
    else:
        if execute is None:
            raise ValueError('command executor required')
        execute('check', result)
        for name in names:
            value = secret_fn(name + ' token: ').encode()
            if not value: raise ValueError('secret must not be empty')
            execute('secret', (platform, sid, refs[name], value))
        execute('source', source)
        execute('route', route)
        execute('enable', sid)
        execute('doctor', None)
        result['install_service'] = input_fn('Install service? (y/N): ').strip().lower() == 'y'
        if result['install_service']: execute('service', 'install')
        print('Send a test message and check channels service status and channels messages.', file=output)
    return result
