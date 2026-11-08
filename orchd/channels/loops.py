"""Server-owned automation facts; platform message metadata is never trusted."""
from dataclasses import dataclass

@dataclass(frozen=True)
class Automation:
    app_ref: str = ''
    self_echo: bool = False
    verified: bool = False
    correlation_id: str = ''
    causation_id: str = ''
    origin_host: str = ''
    target_host: str = ''
    hop_count: int = 0


def guard(context, *, state=None, sent_echo=False):
    """Pure policy; the repository supplies durable facts inside the claim transaction."""
    from .models import canonical
    if context is None:
        context = Automation()
    if not isinstance(context, Automation) or type(context.hop_count) is not int or context.hop_count < 0:
        raise ValueError('invalid automation context')
    if any(not isinstance(getattr(context,name),str) for name in ('app_ref','correlation_id','causation_id','origin_host','target_host')) or type(context.verified) is not bool or type(context.self_echo) is not bool:
        raise ValueError('invalid automation context')
    provenance = canonical(dict(app_ref=context.app_ref, verified=context.verified,
        correlation_id=context.correlation_id, causation_id=context.causation_id,
        origin_host=context.origin_host, target_host=context.target_host, hop_count=context.hop_count))
    if len(provenance.encode()) > 20000:
        raise ValueError('automation context exceeds limit')
    if context.self_echo:
        return 'sent_echo', provenance
    if sent_echo:
        return 'sent_echo', provenance
    if not context.app_ref and not context.verified and not context.correlation_id:
        return 'human', provenance
    if not context.verified or not context.correlation_id:
        return 'unverified_automation', provenance
    if context.target_host:
        # Multi-host peer delivery is deferred; no default lane may impersonate it.
        status = 'peer_disabled'
    elif context.hop_count >= 8:
        status = 'hop_limit'
    elif state and (state['paused'] or state['turns'] >= 4):
        status = 'budget_exhausted'
    else:
        status = 'allowed'
    return status, provenance
