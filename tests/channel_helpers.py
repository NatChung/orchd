import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from orchd import store
from orchd.channels import policy
from orchd.channels.service import ChannelService

NOW = datetime(2026, 1, 2, tzinfo=timezone.utc)

class ChannelCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'db'
        con = store.connect(self.path)
        con.execute("INSERT INTO orchs(id,kind,created_at) VALUES('orch','codex',0)")
        con.close()
        self.open()
        self.addCleanup(lambda: self.svc.close())
        self.op = policy.Caller(role='operator')
    def open(self):
        self.svc = ChannelService(self.path, clock=lambda: NOW, channel_home=Path(self.tmp.name)/'channel')
    def source(self, sid='s', provider='slack', account=None):
        self.svc.provision_source(dict(id=sid, host_id='h', provider=provider, tenant_ref='t', account_ref=account or sid, adapter_id=sid, enabled=True, credential_refs={'signing':'os-store:line/signing','app':'os-store:slack/app','api':'os-store:slack/api'}), caller=self.op)
        self.svc.apply_routes([dict(id='r-'+sid, source_id=sid, conversation_ref='c', owner_orch='orch', mode='receive_only')], caller=self.op)
        return policy.Caller(role='adapter', sources=frozenset({sid}), identity=sid)
    def event(self, key='e', mid='m'):
        return dict(schema_version=1, transport_event_key=key, origin=dict(provider='slack',tenant_ref='t',conversation_ref='c',message_id=mid), revision_key='initial', conversation=dict(kind='channel',thread_key=''), sender_ref='human',source_time=NOW.isoformat(),content=dict(status='complete',text='synthetic',attachments=[]))
