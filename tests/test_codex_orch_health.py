"""Codex probes use isolated registry/socket fixtures; never contact a real Orch."""
import contextlib
import io
import json
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import psutil

from orchd import app_worker, cli, codex_orch, interface, inventory, store
from orchd.orch_health import owner_health
from tests.test_orchd import FakeRuntime


class CodexOrchHealthTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name).resolve()
        self.directory.chmod(0o700)
        self.con = store.connect(self.directory / 'orchd.db')
        self.addCleanup(self.con.close)
        store.register_orch(self.con, 'thread', 'codex')
        self.endpoint = str(self.directory / 'app.sock')
        self.socket = socket.socket(socket.AF_UNIX)
        self.socket.bind(self.endpoint)
        self.addCleanup(self.socket.close)
        self.spec = dict(backend='app-server', endpoint=self.endpoint, thread_id='thread',
                         server_identity=app_worker.identity(os.getpid()))
        self.rpc = Mock()
        self.rpc.call.return_value = {'thread': {'id': 'thread', 'status': {'type': 'active'}}}
        self.patch = patch.object(codex_orch, 'RPC', return_value=self.rpc)
        self.factory = self.patch.start()
        self.addCleanup(self.patch.stop)
        self.rt = FakeRuntime()
        self.save()

    def save(self):
        self.con.execute('UPDATE orchs SET codex_probe=? WHERE id=?', (json.dumps(self.spec), 'thread'))

    def health(self):
        return owner_health(store.get_orch(self.con, 'thread'), None)

    def assertHealth(self, state, reason):
        self.assertEqual(self.health(), dict(state=state, reason=reason))

    def test_active_and_idle_require_matching_thread_and_live_identity(self):
        for status, state in (('active', 'alive'), ('idle', 'idle')):
            with self.subTest(status=status):
                self.rpc.call.return_value['thread']['status']['type'] = status
                self.assertHealth(state, 'codex_thread_' + status)
                self.rpc.call.assert_called_with('thread/read', {'threadId': 'thread'}, timeout=3)
                self.rpc.close.assert_called()

    def test_confirmed_process_gone_never_connects(self):
        with patch.object(app_worker.psutil, 'Process', side_effect=psutil.NoSuchProcess(123)):
            self.assertHealth('dead', 'codex_process_gone')
        self.factory.assert_not_called()

    def test_pid_reuse_does_not_probe_unrelated_process(self):
        data = json.loads(self.spec['server_identity'])
        data['birth'] -= 1
        self.spec['server_identity'] = json.dumps(data)
        self.save()
        self.assertHealth('dead', 'codex_process_gone')
        self.factory.assert_not_called()

    def test_unreadable_process_identity_is_unknown(self):
        with patch.object(app_worker.psutil, 'Process', side_effect=psutil.AccessDenied(123)):
            self.assertHealth('unknown', 'codex_process_unavailable')
        self.factory.assert_not_called()

    def test_process_exit_during_rpc_does_not_leave_alive(self):
        with patch.object(app_worker, 'process', side_effect=[Mock(), None]):
            self.assertHealth('dead', 'codex_process_gone')

    def test_socket_connection_timeout_and_rpc_error_are_unknown_and_redacted(self):
        for error in (ConnectionRefusedError('SECRET'), TimeoutError('SECRET'), RuntimeError('SECRET')):
            with self.subTest(error=error), patch.object(codex_orch, 'RPC', side_effect=error):
                self.assertHealth('unknown', 'codex_socket_unavailable')
        self.rpc.call.side_effect = RuntimeError('SECRET')
        self.assertHealth('unknown', 'codex_socket_unavailable')
        self.rpc.close.assert_called()

    def test_private_socket_missing_public_directory_and_symlink_are_unknown(self):
        for mode in (0o755, 0o777):
            self.directory.chmod(mode)
            self.assertHealth('unknown', 'codex_socket_unavailable')
        self.directory.chmod(0o700)
        link = self.directory / 'link.sock'
        link.symlink_to(self.endpoint)
        self.spec['endpoint'] = str(link)
        self.save()
        self.assertHealth('unknown', 'codex_socket_unavailable')
        self.spec['endpoint'] = str(self.directory / 'missing.sock')
        self.save()
        self.assertHealth('unknown', 'codex_socket_unavailable')
        self.factory.assert_not_called()

    def test_thread_mismatch_and_unrecognized_status_are_unknown(self):
        self.rpc.call.return_value['thread']['id'] = 'other'
        self.assertHealth('unknown', 'codex_thread_mismatch')
        self.rpc.call.return_value['thread']['id'] = 'thread'
        for status in ('notLoaded', 'systemError', 'future'):
            self.rpc.call.return_value['thread']['status']['type'] = status
            self.assertHealth('unknown', 'codex_thread_status_unsupported')
        self.rpc.call.return_value = {'thread': {}}
        self.assertHealth('unknown', 'codex_probe_invalid')

    def test_malformed_registration_never_proves_death(self):
        for raw in ('{}', 'null', 'bad json', json.dumps({**self.spec, 'server_identity': '{}'}),
                    json.dumps({**self.spec, 'thread_id': 'other'}),
                    json.dumps({**self.spec, 'backend': 'exec'})):
            with self.subTest(raw=raw):
                self.con.execute('UPDATE orchs SET codex_probe=?', (raw,))
                self.assertHealth('unknown', 'codex_probe_invalid')
        self.factory.assert_not_called()

    def test_desktop_and_exec_between_turns_never_claim_dead(self):
        self.con.execute('UPDATE orchs SET codex_probe=NULL,job_id=NULL,stopped_at=1')
        for jobs in (None, {}, {'thread': {'pid': 123, 'status': 'busy'}}):
            self.assertEqual(owner_health(store.get_orch(self.con, 'thread'), jobs),
                             dict(state='unknown', reason='codex_probe_unsupported'))
        row = inventory.list_orchs(self.con, self.rt, observe=True)['orchs'][0]
        self.assertIsNone(row['first_seen_dead'])
        self.assertEqual(row['capabilities']['health_probe'], 'unsupported')
        self.assertFalse(row['capabilities']['process_identity'])

    def test_json_inventory_exposes_reason_and_capabilities_without_probe_secrets(self):
        self.rpc.call.return_value['thread']['status']['type'] = 'idle'
        report = inventory.list_orchs(self.con, self.rt)
        row = report['orchs'][0]
        self.assertEqual(row['health_reason'], 'codex_thread_idle')
        self.assertEqual(row['capabilities'], dict(health_probe='codex_app_server', process_identity=True,
                                                  private_socket=True, thread_status=True))
        self.assertNotIn(self.endpoint, json.dumps(report))
        self.assertNotIn('server_identity', json.dumps(report))
        # Actual command path, with its own temporary connection and FakeRuntime.
        with patch.dict(os.environ, {'ORCHD_HOME': str(self.directory)}), \
                patch.object(cli, 'Runtime', return_value=self.rt), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(['orch', 'list', '--json']), 0)
        self.assertEqual(json.loads(out.getvalue())['orchs'][0]['health_reason'], 'codex_thread_idle')

    def test_registration_cli_captures_identity_and_rejects_unverified_server(self):
        self.con.execute('UPDATE orchs SET codex_probe=NULL')
        with patch.dict(os.environ, {'ORCHD_HOME': str(self.directory)}), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['orch', 'register-codex-probe', 'thread', '--pid', str(os.getpid()),
                                       '--endpoint', self.endpoint]), 0)
        spec = json.loads(store.get_orch(self.con, 'thread')['codex_probe'])
        self.assertEqual(json.loads(spec['server_identity'])['pid'], os.getpid())
        before = spec
        self.rpc.call.return_value['thread']['id'] = 'wrong'
        with self.assertRaisesRegex(ValueError, 'codex_thread_mismatch'):
            codex_orch.register(self.con, 'thread', os.getpid(), self.endpoint)
        self.assertEqual(json.loads(store.get_orch(self.con, 'thread')['codex_probe']), before)

    def test_unknown_health_does_not_gate_interface_delivery(self):
        self.con.execute('UPDATE orchs SET codex_probe=NULL')
        registration = interface.register(self.con, name='test', orch_id='thread')
        result = interface.submit(self.con, self.rt, name='test', token=registration['token'],
                                  conversation='test', request_key='one', text='do it')
        self.assertIsNotNone(result)
        self.assertHealth('unknown', 'codex_probe_unsupported')

    def test_claude_probe_is_unchanged_and_never_uses_codex_rpc(self):
        store.register_orch(self.con, 'claude', 'claude', job_id='job')
        orch = store.get_orch(self.con, 'claude')
        self.assertEqual(owner_health(orch, {'job': {'pid': 42, 'status': 'idle'}}),
                         dict(state='alive', reason='job_present'))
        self.assertEqual(owner_health(orch, {}), dict(state='dead', reason='job_absent'))
        self.assertEqual(owner_health(orch, None), dict(state='unknown', reason='runtime_unavailable'))
        self.factory.assert_not_called()
