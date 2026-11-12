from pathlib import Path
from unittest.mock import patch

from orchd import config
from tests.channel_helpers import ChannelCase


class NotificationWindowTest(ChannelCase):
    def setUp(self):
        super().setUp()
        self.config_path = Path(self.tmp.name) / 'config.toml'
        self.env = patch.dict('os.environ', {'ORCHD_CONFIG_FILE': str(self.config_path)})
        self.env.start()
        self.addCleanup(self.env.stop)

    def configure(self, text):
        self.config_path.write_text(text, encoding='utf-8')

    def receive(self, sid='s', key='e'):
        adapter = self.source(sid)
        result = self.svc.receive(sid, self.event(key, key), caller=adapter)
        row = self.svc.con.execute('SELECT * FROM intake_notifications WHERE receipt_id=?', (result['receipt_id'],)).fetchone()
        return adapter, result, dict(row)

    def test_sources_have_independent_windows(self):
        self.configure('[channels.notify.s]\nwindow_seconds = 0\n[channels.notify.other]\nwindow_seconds = 42\n')
        _, _, first = self.receive('s')
        _, _, second = self.receive('other', 'other')
        self.assertEqual(first['due_at'] - first['created_at'], 0)
        self.assertEqual(second['due_at'] - second['created_at'], 42)

    def test_zero_window_due_at_received_time(self):
        self.configure('[channels.notify.s]\nwindow_seconds = 0\n')
        _, _, row = self.receive()
        self.assertEqual(row['due_at'], row['created_at'])

    def test_replay_is_idempotent(self):
        self.configure('[channels.notify.s]\nwindow_seconds = 0\n')
        adapter, result, row = self.receive()
        self.assertEqual(self.svc.receive('s', self.event('e', 'e'), caller=adapter)['status'], 'duplicate')
        rows = self.svc.con.execute('SELECT * FROM intake_notifications WHERE receipt_id=?', (result['receipt_id'],)).fetchall()
        self.assertEqual([dict(r) for r in rows], [row])

    def test_unconfigured_source_defaults_to_1800(self):
        _, _, row = self.receive()
        self.assertEqual(row['due_at'] - row['created_at'], 1800)

    def test_invalid_windows_are_rejected(self):
        for value in ('-1', 'true', '1.5', '"1"'):
            with self.subTest(value=value):
                self.configure('[channels.notify.s]\nwindow_seconds = ' + value + '\n')
                with self.assertRaises(ValueError):
                    config.channel_notify_window('s')
        for text in ('channels = 1', '[channels]\nnotify = 1', '[channels.notify]\ns = 1', '[channels.notify.s]\nother = 1'):
            with self.subTest(text=text):
                self.configure(text)
                with self.assertRaises(ValueError):
                    config.channel_notify_window('s')
