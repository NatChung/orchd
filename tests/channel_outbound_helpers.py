"""Synthetic outbound fixtures; Entry and SQLite use their real public APIs."""
from datetime import timedelta
from orchd import core, entry
from orchd.channels import policy
from tests.channel_helpers import ChannelCase, NOW
from tests.test_entry import EntryRuntime


class BeforeClaimUpdate:
    """Test-only scheduling seam; all SQLite operations still hit the real DB."""
    def __init__(self, connection, hook):
        self.connection, self.hook = connection, hook

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def execute(self, sql, parameters=()):
        if sql.startswith("UPDATE channel_deliveries SET state='claimed'") and self.hook:
            hook, self.hook = self.hook, None
            hook()
        return self.connection.execute(sql, parameters)


class OutboundCase(ChannelCase):
    def setUp(self):
        super().setUp()
        self.now = NOW
        self.svc.clock = lambda: self.now
        self.rt = EntryRuntime()
        self.owner = core.start_orch(self.svc.con, self.rt, 'opus')['id']
        entry.bind(self.svc.con, self.rt, self.owner)
        entry.touch(self.svc.con, 'desktop', 'synthetic-desktop')
        self.adapter = self.source()
        self.routes()
        self.op = policy.Caller(role='operator', sources=frozenset({'s'}))
        self.worker = policy.Caller(role='orch', sources=frozenset({'s'}), identity=self.owner)
        self.mid = self.svc.receive('s', self.event(), caller=self.adapter)['message_id']
        self.enable()

    def routes(self, **changes):
        route = dict(id='r-s', source_id='s', conversation_ref='c', owner_orch=self.owner,
                     responder_source_id='s', mode='respond')
        route.update(changes)
        self.svc.apply_routes([route], caller=self.op)

    def enable(self, enabled=True, paused=False):
        self.svc.channel_home.mkdir(parents=True, exist_ok=True)
        (self.svc.channel_home / 'channels.toml').write_text(
            '[channels]\noutbound_enabled = ' + str(enabled).lower() + '\npaused = ' + str(paused).lower() + '\n')

    def draft(self, **changes):
        payload = dict(sender='s', target='c', body='Synthetic reply', expires_at=(self.now+timedelta(hours=1)).timestamp())
        payload.update(changes)
        return self.svc.draft_reply(self.mid, payload, caller=self.worker)

    def answer(self, draft, text=None, preview=None):
        shown = self.svc.approval_preview(draft['draft_id'], draft['version'], caller=self.op)
        question = entry.ask_nat(self.svc.con, self.rt, self.owner, preview or shown['preview'])['question_id']
        return entry.submit_answer(self.svc.con, self.rt, 'desktop', question, 1, 'current',
                                  text or shown['answer'], 'answer-'+str(question), orch_id=self.owner)['message_id']

    def approve(self, draft=None, **changes):
        draft = draft or self.draft()
        args = dict(draft_id=draft['draft_id'], version=draft['version'], payload_hash=draft['payload_hash'],
                    entry_answer_ref=self.answer(draft), caller=self.op)
        args.update(changes)
        return self.svc.approve(**args)
