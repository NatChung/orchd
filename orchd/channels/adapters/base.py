"""Platform boundaries. An adapter credential never conveys approval authority."""
from typing import Protocol

class MailTransport(Protocol):
    def list_messages(self, start, end, page_token=None): ...
    def get_message(self, message_id): ...
    def get_attachment(self, message_id, attachment_id): ...


class SendTransport(Protocol):
    def send(self, payload: dict, idempotency_key: str) -> str: ...


class OutboundAdapter:
    """Source-scoped secret loading followed by the common claim/send gate.

    A send transport factory is deliberately required. Receive transports are
    never implicitly reused (especially a Gmail read credential). Constructing
    the adapter does not load credentials, import an SDK, claim or send.
    """
    def __init__(self, service, source_id, caller, secret_store, transport_factory):
        self.service, self.source_id, self.caller = service, source_id, caller
        self.secret_store, self.transport_factory = secret_store, transport_factory

    def sweep(self):
        from .. import policy
        from ..secret_store import resolve
        from ..delivery import OutboundRunner
        policy.require_outbound_enabled(self.service.channel_home)
        source = self.service.adapter_source(self.source_id, caller=self.caller)
        refs = source['credential_refs']
        if source['provider'] == 'gmail':
            policy.require_gmail_send_enabled(self.service.channel_home, self.source_id)
            reference = refs.get('send')
            if not reference or reference == refs.get('read'):
                raise PermissionError('separate Gmail send credential required')
        else:
            reference = refs.get('api')
        material = resolve(reference, adapter=self.caller.identity, source=self.source_id, provider=self.secret_store)
        # Factory is the adapter's only platform-specific dependency; no secret
        # is exposed in the claim payload or persisted in the delivery ledger.
        transport = self.transport_factory(material, source['account_ref'])
        return OutboundRunner(self.service, self.source_id, self.caller, transport).sweep()
