import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from orchd import cli
from orchd.channels.service import ChannelService


def snapshot(root):
    return {str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in root.rglob('*') if p.is_file()}


class HealthTest(unittest.TestCase):
    def test_live_wal_health_is_metadata_only_and_does_not_expire_content(self):
        from datetime import datetime, timezone
        from orchd.channels import policy
        from orchd.channels.health import status
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            path = root / 'db'
            svc = ChannelService(path, clock=lambda: datetime(2026,1,1,tzinfo=timezone.utc))
            try:
                svc.provision_source(dict(id='demo-source',host_id='demo-host',provider='gmail',account_ref='demo-account',adapter_id='demo-adapter',enabled=True),caller=policy.Caller(role='operator'))
                before = snapshot(root)
                with patch('socket.socket', side_effect=AssertionError('network')):
                    report = status(path, home=root, env={'ORCHD_HOME':str(root/'state'),'ORCHD_ROOT':str(root/'orch')})
                self.assertEqual(snapshot(root), before)
                self.assertEqual(report['sources'][0]['adapter_owner'], 'demo-adapter')
                self.assertEqual(report['sources'][0]['credential_status'], 'unavailable')
                for name in ('last_success','freshness','pending','uncertain','retention_gap','credential_error'):
                    self.assertIn(name, report['sources'][0])
                self.assertNotIn('demo-account', str(report))
            finally:
                svc.close()

    def test_status_doctor_and_layout_plan_never_write_fresh_or_populated(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            env = {k: str(root / k) for k in ('HOME', 'USERPROFILE', 'ORCHD_HOME', 'ORCHD_ROOT', 'APPDATA', 'LOCALAPPDATA')}
            env['USERNAME'] = 'synthetic'
            for populated in (False, True):
                if populated:
                    svc = ChannelService(root / 'ORCHD_HOME' / 'orchd.db')
                    svc.close()
                before = snapshot(root)
                with patch.dict(os.environ, env, clear=True), patch('socket.socket', side_effect=AssertionError('network')):
                    for action in ('status', 'doctor'):
                        out = io.StringIO()
                        with contextlib.redirect_stdout(out):
                            self.assertEqual(cli.main(['channels', action, '--json']), 0)
                        import json
                        report = json.loads(out.getvalue())
                        self.assertIn('layout_version', report)
                        self.assertEqual(report['sources'], [])
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(cli.main(['channels', 'layout', 'plan']), 0)
                self.assertEqual(snapshot(root), before)
