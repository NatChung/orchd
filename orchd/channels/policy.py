"""Authorization context supplied by trusted local entry points, never by payloads."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Caller:
    role: str
    sources: frozenset = frozenset()
    identity: str = ''
    owner_orch: str = ''


def require_source(caller, source_id):
    if not isinstance(caller, Caller) or source_id not in caller.sources:
        raise PermissionError('source access denied')


def require_operator(caller):
    if not isinstance(caller, Caller) or caller.role != 'operator':
        raise PermissionError('operator authority required')


def require_message(caller, row):
    require_source(caller, row['source_id'])
    owner = row.get('owner_orch')
    if owner and caller.role != 'operator' and owner not in (caller.identity, caller.owner_orch):
        raise PermissionError('message owner access denied')

# Retention is per source; values are days. A tombstone is retained after content removal.
DEFAULT_BODY_DAYS = 90
DEFAULT_ATTACHMENT_DAYS = 90
MAX_BACKUP_DAYS = 30


def caller_for_identity(identity, *, config=None):
    """Resolve grants from local trusted config, never from client request JSON."""
    if not identity:
        raise PermissionError('caller identity required')
    if config is None:
        from orchd import config as settings
        config = settings._read()[1].get('channels', {})
    if not isinstance(config, dict) or not isinstance(config.get('acl', {}), dict):
        raise ValueError('invalid channel ACL configuration')
    grants=config.get('acl', {}).get(identity, [])
    if not isinstance(grants, list) or not all(isinstance(x,str) and x for x in grants):
        raise ValueError('invalid channel source grants')
    return Caller(role='orch', sources=frozenset(grants), identity=identity)


def retention_policies(config=None):
    if config is None:
        from orchd import config as settings
        config = settings._read()[1].get('channels', {})
    if not isinstance(config, dict) or not isinstance(config.get('retention', {}), dict):
        raise ValueError('invalid channel retention configuration')
    policies={}
    for source_id, value in config.get('retention', {}).items():
        if not isinstance(value, dict) or set(value) - {'body_days','attachment_days','backup_days'}:
            raise ValueError('invalid source retention policy')
        body=value.get('body_days',DEFAULT_BODY_DAYS)
        attachments=value.get('attachment_days',DEFAULT_ATTACHMENT_DAYS)
        backup=value.get('backup_days',MAX_BACKUP_DAYS)
        if any(type(x) is not int or x < 0 for x in (body,attachments,backup)) or backup > MAX_BACKUP_DAYS:
            raise ValueError('invalid source retention duration')
        if body != attachments:
            raise ValueError('body and attachment retention must match')
        policies[source_id]={'body_days':body,'attachment_days':attachments,'backup_days':backup}
    return policies


def caller_for_worker(con, worktree):
    from . import repository
    task=repository.worker_identity(con,worktree)
    granted=caller_for_identity('worker:'+task['id'])
    return Caller(role='worker',sources=granted.sources,identity=granted.identity,owner_orch=task['owner_orch'])
