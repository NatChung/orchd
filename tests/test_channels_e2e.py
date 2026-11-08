import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from orchd import channel_layout, cli
from tests.test_channels_health import snapshot


class E2ETest(unittest.TestCase):
    def run_cli(self, args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(args), 0, out.getvalue())
        return out.getvalue()

    def test_install_upgrade_resume_and_rollback_preserve_unacked_spool_and_user_change(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            env = {k: str(root / k) for k in ('HOME', 'USERPROFILE', 'ORCHD_HOME', 'ORCHD_ROOT', 'APPDATA', 'LOCALAPPDATA')}
            env['USERNAME'] = 'synthetic'
            with patch.dict(os.environ, env, clear=True), patch('socket.socket', side_effect=AssertionError('network')):
                before = snapshot(root)
                self.run_cli(['install', '--dry-run', '--no-trust', '--json'])
                self.run_cli(['upgrade', '--dry-run'])
                self.assertEqual(snapshot(root), before)
                installed = json.loads(self.run_cli(['install', '--no-trust', '--json']))
                from orchd.channels import repository
                original = repository.connect
                with patch.object(repository, 'connect', side_effect=OSError('synthetic schema interruption')):
                    with contextlib.redirect_stderr(io.StringIO()):
                        self.assertEqual(cli.main(['upgrade', '--migrations-only']), 1)
                self.run_cli(['upgrade', '--migrations-only'])
                db = original(root / 'ORCHD_HOME/orchd.db')
                db.close()
                before = snapshot(root)
                self.run_cli(['upgrade', '--migrations-only'])
                self.assertEqual(snapshot(root), before)
                layout = root / 'ORCHD_ROOT/channel'
                for provider in ('email', 'slack', 'line'):
                    self.assertTrue((layout / provider / 'state').is_dir())
                changed = layout / 'slack/config.toml'
                changed.write_bytes(b'# synthetic user change\n')
                from orchd.channels.spool import Spool
                spool = layout / 'line/state/unacked.spool'
                queue = Spool(spool, 'demo-source')
                queue.append([{'transport_event_key':'demo-event','content':{'text':'synthetic-unacked-event'}}])
                pending = queue.pending()
                queue.close()
                homes = snapshot(root / 'ORCHD_ROOT/home'), snapshot(root / 'ORCHD_ROOT/interface')
                result = channel_layout.rollback(installed['channel_layout']['migration_id'], apply=True)
                self.assertIn(str(changed), result['conflicts'])
                self.assertEqual(changed.read_bytes(), b'# synthetic user change\n')
                self.assertTrue(spool.is_file())
                queue = Spool(spool, 'demo-source')
                try:
                    self.assertEqual(queue.pending(), pending)
                finally:
                    queue.close()
                self.assertEqual(homes, (snapshot(root / 'ORCHD_ROOT/home'), snapshot(root / 'ORCHD_ROOT/interface')))

    def test_layout_interruption_resumes_before_schema_receipt(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            env = {'ORCHD_ROOT':str(root / 'orch'), 'ORCHD_HOME':str(root / 'state')}
            real = channel_layout._atomic
            count = 0
            def interrupted(path, data):
                nonlocal count
                count += 1
                if count == 2:
                    raise OSError('synthetic layout interruption')
                real(path, data)
            with patch.object(channel_layout, '_atomic', side_effect=interrupted):
                with self.assertRaises(OSError):
                    channel_layout.provision(home=root, env=env)
            self.assertFalse((root / 'state/orchd.db').exists())
            first = channel_layout.provision(home=root, env=env)
            second = channel_layout.provision(home=root, env=env)
            self.assertEqual(first['migration_id'], second['migration_id'])
            from orchd import store
            con = store.connect_readonly(root / 'state/orchd.db')
            try:
                self.assertEqual([r[0] for r in con.execute('SELECT version FROM orchd_channel_migrations ORDER BY version')], [1,2])
            finally:
                con.close()
