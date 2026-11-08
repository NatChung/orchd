"""Single-host cutover rehearsals, using only synthetic local records."""
from tests.channel_helpers import ChannelCase
from tests.channel_outbound_helpers import OutboundCase


class CutoverTest(ChannelCase):
    def setUp(self):
        super().setUp()
        self.adapter = self.source()

    def test_shadow_has_no_work_then_replay_takes_over_once(self):
        self.svc.cutover('s', 'shadow', caller=self.op)
        result = self.svc.receive('s', self.event(), caller=self.adapter)
        report = self.svc.cutover_ledger('s', caller=self.op)
        self.assertEqual(report['work'], [])
        self.assertEqual(report['deliveries'], [])
        self.svc.cutover('s', 'frozen', caller=self.op)
        self.svc.cutover('s', 'adapter', caller=self.op)
        replay = self.svc.receive('s', self.event(), caller=self.adapter)
        self.assertEqual(replay['message_id'], result['message_id'])
        self.svc.receive('s', self.event(), caller=self.adapter)
        self.assertEqual(len(self.svc.cutover_ledger('s', caller=self.op)['work']), 1)

    def test_old_polling_and_adapter_cannot_overlap(self):
        self.svc.cutover('s', 'frozen', caller=self.op)
        self.svc.cutover('s', 'legacy', caller=self.op)
        with self.assertRaises(PermissionError):
            self.svc.acquire_source_lease('s', 'new-runner', caller=self.adapter)
        with self.assertRaises(PermissionError):
            self.svc.receive('s', self.event(), caller=self.adapter)
        self.svc.cutover('s', 'frozen', caller=self.op)
        self.svc.cutover('s', 'adapter', caller=self.op)
        from orchd.channels import repository
        with self.assertRaises(PermissionError):
            repository.require_admission(self.svc.con, 's', 'legacy')

    def test_active_runner_must_release_before_fencing(self):
        generation = self.svc.acquire_source_lease('s', 'runner', caller=self.adapter)
        with self.assertRaises(ValueError):
            self.svc.cutover('s', 'frozen', caller=self.op)
        self.svc.release_source_lease('s', 'runner', generation, caller=self.adapter)
        self.svc.cutover('s', 'frozen', caller=self.op)

    def test_legacy_intake_and_interface_entry_points_obey_fence(self):
        from orchd import intake, interface
        self.svc.cutover('s', 'frozen', caller=self.op)
        self.svc.cutover('s', 'adapter', caller=self.op)
        event = dict(version=1,provider='slack',account='s',event_id='old',source_ref='c',source_time=1700000000,summary='synthetic')
        grant = {'s': {'providers':['slack'], 'accounts':['s']}}
        with self.assertRaises(PermissionError):
            intake.submit(self.svc.con, event, producer='s', producers=grant)
        v2 = {**event,'version':2,'content':{'status':'complete','text':'synthetic','attachments':[]}}
        with self.assertRaises(PermissionError):
            intake.submit(self.svc.con, v2, producer='s', producers=grant)
        registration = interface.register(self.svc.con, name='s', orch_id='orch')
        with self.assertRaises(PermissionError):
            interface.submit(self.svc.con, None, name='s', token=registration['token'], conversation='c',request_key='old',text='synthetic')

    def test_worker_cannot_change_fence_or_export_control_ledger(self):
        from orchd.channels.policy import Caller
        worker = Caller(role='worker', sources=frozenset({'s'}))
        with self.assertRaises(PermissionError):
            self.svc.cutover('s', 'frozen', caller=worker)
        with self.assertRaises(PermissionError):
            self.svc.cutover_ledger('s', caller=worker)

    def test_fixed_high_water_and_incremental_backfill_keep_new_events(self):
        first = self.svc.receive('s', self.event(), caller=self.adapter)
        fixed = self.svc.cutover_ledger('s', caller=self.op)
        second = self.svc.receive('s', self.event('e2', 'm2'), caller=self.adapter)
        historical = self.svc.cutover_ledger('s', through=fixed['high_water'], caller=self.op)
        delta = self.svc.cutover_ledger('s', after=fixed['high_water'], caller=self.op)
        self.assertEqual([m['id'] for m in historical['messages']], [first['message_id']])
        self.assertEqual([m['id'] for m in delta['messages']], [second['message_id']])
        self.svc.cutover('s', 'frozen', caller=self.op)
        with self.assertRaises(ValueError):
            self.svc.cutover('s', 'legacy', backfill=fixed['digest'], caller=self.op)
        latest = self.svc.cutover_ledger('s', caller=self.op)
        self.svc.cutover('s', 'legacy', backfill=latest['digest'], caller=self.op)
        self.assertEqual(self.svc.cutover_ledger('s', caller=self.op)['messages'], latest['messages'])


class UnknownCutoverTest(OutboundCase):
    def test_unknown_send_never_resent_across_cutover_and_restart(self):
        approved = self.approve()
        claim = self.svc.claim_outbound('s', caller=self.adapter)[0]
        self.svc.cutover('s', 'frozen', caller=self.op)
        self.svc.close()
        self.open()
        ledger = self.svc.cutover_ledger('s', caller=self.op)
        outbound = [d for d in ledger['deliveries'] if d['kind'] == 'outbound']
        self.assertEqual([d['id'] for d in outbound], [approved['delivery_id']])
        self.svc.cutover('s', 'adapter', caller=self.op)
        for _ in range(3):
            self.assertEqual(self.svc.claim_outbound('s', caller=self.adapter), [])
        view = self.svc.delivery(claim['id'], caller=self.op)
        self.assertEqual(view['state'], 'uncertain')
        self.assertEqual(len(view['attempts']), 1)
        self.svc.cutover('s', 'frozen', caller=self.op)
        with self.assertRaises(ValueError):
            self.svc.cutover('s', 'legacy', backfill=ledger['digest'], caller=self.op)
