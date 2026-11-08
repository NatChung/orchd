"""Validation for route manifests."""

def validate_routes(routes):
    if not isinstance(routes, list):
        raise ValueError('routes must be a list')
    ids = set()
    for route in routes:
        allowed = {'id', 'source_id', 'conversation_ref', 'owner_orch', 'senders', 'project_ref',
                   'repo_ref', 'responder_source_id', 'mode', 'authority_epoch'}
        if not isinstance(route, dict) or set(route) - allowed:
            raise ValueError('invalid route fields')
        for key in ('id', 'source_id', 'conversation_ref', 'owner_orch'):
            if not isinstance(route.get(key), str) or not route[key]:
                raise ValueError('invalid route identity')
        if route['id'] in ids:
            raise ValueError('duplicate route id')
        ids.add(route['id'])
        senders = route.get('senders', [])
        if not isinstance(senders, list) or not all(isinstance(x, str) and x for x in senders) or len(senders) != len(set(senders)):
            raise ValueError('invalid sender scope')
        if route.get('mode', 'receive_only') not in ('receive_only', 'respond', 'observer', 'peer'):
            raise ValueError('invalid route mode')
        if type(route.get('authority_epoch', 1)) is not int or route.get('authority_epoch', 1) < 1:
            raise ValueError('invalid authority epoch')
    for i, a in enumerate(routes):
        for b in routes[i + 1:]:
            if a['source_id'] != b['source_id']:
                continue
            if a['conversation_ref'] != b['conversation_ref'] and '*' not in (a['conversation_ref'], b['conversation_ref']):
                continue
            left, right = set(a.get('senders', [])), set(b.get('senders', []))
            if not left or not right or left & right:
                raise ValueError('overlapping routes')
