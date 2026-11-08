import unittest
import tempfile
from pathlib import Path
from orchd.channels.spool import Spool

class SpoolTest(unittest.TestCase):
    def test_lost_ack_replays_and_keyset_survives_restart(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'spool'
            s=Spool(p,'source',clock=lambda:'2026-01-01T00:00:00Z')
            s.append([{'transport_event_key':'b','value':2},{'transport_event_key':'a','value':1}])
            page=s.pending(limit=1); self.assertEqual(page[0]['seq'],1)
            s.close(); s=Spool(p,'source')
            self.assertEqual(s.pending(limit=1),page)
            s.ack(1); self.assertEqual(s.pending()[0]['seq'],2)
            s.close()
    def test_conflict_batch_is_atomic(self):
        with tempfile.TemporaryDirectory() as d:
            s=Spool(Path(d)/'spool','source')
            s.append([{'transport_event_key':'a','value':1}])
            with self.assertRaises(ValueError):
                s.append([{'transport_event_key':'b'},{'transport_event_key':'a','value':2}])
            self.assertEqual(len(s.pending()),1)
            s.close()
    def test_capacity_signals_gap_and_source_binding_is_enforced(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'spool'; s=Spool(p,'s',max_events=1)
            try:
                s.append([{'transport_event_key':'a'}])
                with self.assertRaisesRegex(ValueError,'retention_gap'): s.append([{'transport_event_key':'b'}])
                with self.assertRaises(PermissionError): Spool(p,'other')
                self.assertEqual(len(s.pending()),1)
            finally: s.close()
    def test_unsend_scrubs_pending_and_ack_erases_body(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'spool'; s=Spool(p,'s')
            try:
                origin={'provider':'line','message_id':'m'}
                s.append([{'transport_event_key':'m','origin':origin,'content':{'text':'PRIVATE-SYNTHETIC'}}])
                s.append([{'transport_event_key':'u','origin':origin,'content':{'status':'unavailable','text':None,'unavailable_reason':'deleted'}}])
                self.assertIsNone(s.pending()[0]['event']['content']['text'])
                self.assertNotIn(b'PRIVATE-SYNTHETIC',p.read_bytes())
                s.ack(1); s.ack(2)
                s.append([{'transport_event_key':'late','origin':origin,'content':{'text':'PRIVATE-SYNTHETIC'}}])
                self.assertIsNone(s.pending()[0]['event']['content']['text'])
            finally: s.close()
