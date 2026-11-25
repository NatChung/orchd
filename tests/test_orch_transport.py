"""Native export discovery is context-bound and never restarts a provider."""
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from orchd import paths, store
from orchd.runtime import Runtime, UDSWriteUncertain


class Process:
    def __init__(self, pid, home, argv=(), env=None, parent=None):
        self.pid, self.home, self.argv, self.env, self.parent = pid, home, list(argv), env or {}, parent
        self.birth = 1000.0 + pid
        self.kids = []
    def create_time(self): return self.birth
    def uids(self): return type('UID', (), {'real': os.getuid(), 'effective': os.getuid()})()
    def cwd(self): return str(self.home)
    def cmdline(self): return self.argv
    def environ(self): return dict(self.env)
    def children(self): return list(self.kids)
    def ppid(self): return self.parent
    def is_running(self): return True
    def status(self): return 'sleeping'


@unittest.skipIf(os.name == 'nt', 'native fallback is Unix only')
class TransportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve()
        self.patch = mock.patch.dict(os.environ, {'ORCHD_HOME': str(self.home / 'state'),
            'ORCHD_ROOT': str(self.home), 'ORCHD_ORCH_HOME': str(self.home),
            'CLAUDE_CONFIG_DIR': str(self.home / 'native'), 'ORCHD_PROJECTS': str(self.home / 'projects'),
            'ORCHD_CONFIG_DIR': str(self.home / 'config')}, clear=True)
        self.patch.start(); self.addCleanup(self.patch.stop)
        self.con = store.connect()
        self.addCleanup(self.con.close)
        self.logical = self.home / 'logical' / 'o.sock';self.logical.parent.mkdir()
        self.orch = store.register_orch(self.con, 'owner', 'claude', socket=str(self.logical),
            session_id='native-session', job_id='native-job', model='exact-model')
        self.orch = dict(store.get_orch(self.con, 'owner'))
        self.receiver = self.home / 'reviewed-old-sdk'; self.receiver.mkdir()
        command, entry = self.receiver / 'python', self.receiver / 'orchd'
        for p in (command, entry):p.write_text('fixture');p.chmod(0o700)
        env = {key:os.environ[key] for key in paths.PROFILE_PATH_VARS if key in os.environ}
        env.update(ORCHD_ORCH_ID='owner',ORCHD_PROJECTS=str(paths.projects_dir()))
        self.config = {'command': str(command), 'args': [str(entry), 'mcp'], 'env': env}
        self.mcp = self.logical.parent / 'mcp.json'
        self.mcp.write_text(json.dumps({'mcpServers': {'orchd': self.config}}));self.mcp.chmod(0o600)
        state = self.home / 'native' / 'jobs' / 'native-job' / 'state.json';state.parent.mkdir(parents=True)
        state.write_text(json.dumps({'sessionId': 'native-session', 'cwd': str(self.home),
            'respawnFlags': ['--model', 'exact-model']}));state.chmod(0o600)
        self.physical = self.home / 'native.sock'
        self.listener = socket.socket(socket.AF_UNIX);self.listener.bind(str(self.physical));self.listener.listen()
        self.physical.chmod(0o600);self.addCleanup(self.listener.close)
        self.native = Process(101, self.home)
        self.child = Process(102, self.home, [str(command), str(entry), 'mcp'], dict(env,
            CLAUDE_CODE_MESSAGING_SOCKET=str(self.physical), CLAUDE_CODE_MESSAGING_TOKEN='synthetic-token',
            CLAUDE_CODE_SESSION_ID='native-session'), 101)
        self.native.kids = [self.child]
        self.rt = Runtime()
        self.job = {'id': 'native-job', 'pid': 101, 'status': 'idle', 'sessionId': 'native-session', 'cwd': str(self.home)}
        self.live = mock.patch.object(self.rt, 'live_jobs', return_value={'native-job': self.job});self.live.start();self.addCleanup(self.live.stop)

    def discovery(self):
        from orchd import orch_transport
        return mock.patch.object(orch_transport.psutil, 'Process', side_effect=lambda pid: {101:self.native,102:self.child}[pid])

    def test_default_channel_sends_auth_then_user_without_native_uuid_claim(self):
        from orchd import orch_transport
        received = []
        def read():
            peer, _ = self.listener.accept()
            with peer:
                stream = peer.makefile('r')
                received.extend(json.loads(stream.readline()) for _ in range(2))
        thread = threading.Thread(target=read);thread.start()
        with self.discovery(), mock.patch.object(orch_transport, '_peer_pid', return_value=101):
            self.assertIsNone(self.rt.send_orch(self.orch, 'local check'))
        thread.join(2);self.assertFalse(thread.is_alive())
        self.assertEqual(received[0], {'type':'auth','token':'synthetic-token'})
        self.assertEqual(received[1]['session_id'], 'native-session')
        self.assertEqual(received[1]['message']['content'], 'local check')
        self.assertNotIn('uuid', received[1])
        self.assertEqual(store.get_orch(self.con, 'owner')['socket'], str(self.logical))

    def test_unknown_identity_never_writes_or_falls_back_to_relaunch(self):
        from orchd import orch_transport
        for change in ('session', 'cwd', 'parent', 'mcp', 'socket-mode', 'listener', 'row', 'ambiguous'):
            with self.subTest(change=change):
                with self.discovery(), mock.patch.object(orch_transport, '_peer_pid', return_value=999 if change=='listener' else 101):
                    self.job['sessionId'] = 'wrong' if change=='session' else 'native-session'
                    self.job['cwd'] = 'relative' if change=='cwd' else str(self.home)
                    self.child.parent = 999 if change=='parent' else 101
                    self.child.env['ORCHD_ORCH_ID'] = 'foreign' if change=='mcp' else 'owner'
                    self.physical.chmod(0o666 if change=='socket-mode' else 0o600)
                    self.native.kids = [self.child,self.child] if change=='ambiguous' else [self.child]
                    self.con.execute('UPDATE orchs SET stopped_at=? WHERE id=?', (1 if change=='row' else None,'owner'))
                    with self.assertRaises(orch_transport.TransportUnavailable):self.rt.send_orch(self.orch,'must not deliver')
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM tasks').fetchone()[0],0)

    def test_health_proof_has_actual_endpoint_and_never_token(self):
        from orchd import orch_transport
        with self.discovery(), mock.patch.object(orch_transport, '_peer_pid', return_value=101):
            info = self.rt.orch_transport_info(self.orch)
        self.assertEqual(info['kind'],'native_exported')
        self.assertEqual(info['socket'],str(self.physical))
        self.assertEqual(info['pid'],101)
        self.assertNotIn('synthetic-token',json.dumps(info))
        self.assertNotIn('token',json.dumps(info))

    def test_model_config_and_process_metadata_fail_closed(self):
        from orchd import orch_transport
        for condition in ('model','config-mode','argv','uid','missing-model'):
            with self.subTest(condition=condition), self.discovery():
                self.job['model'] = 'wrong' if condition=='model' else 'exact-model'
                self.mcp.chmod(0o666 if condition=='config-mode' else 0o600)
                self.child.argv = ['foreign'] if condition=='argv' else [self.config['command'],*self.config['args']]
                changed = dict(self.orch,model=None) if condition=='missing-model' else self.orch
                ids = type('UID',(),{'real':os.getuid()+1,'effective':os.getuid()+1})()
                with mock.patch.object(self.native,'uids',return_value=ids if condition=='uid' else Process.uids(self.native)):
                    with self.assertRaises(orch_transport.TransportUnavailable):orch_transport.discover(self.rt,changed)

    def test_rechecks_process_birth_and_configuration_before_any_write(self):
        from orchd import orch_transport
        with self.discovery():
            endpoint = orch_transport.discover(self.rt,self.orch)
            self.native.birth += 1
            with self.assertRaises(ValueError):endpoint.connect()
            self.native.birth -= 1
            self.child.env['ORCHD_ORCH_ID'] = 'changed'
            with self.assertRaises(ValueError):endpoint.connect()
            self.child.env['ORCHD_ORCH_ID'] = 'owner'
            self.mcp.write_text('{}')
            with self.assertRaises(ValueError):endpoint.connect()

    def test_default_partial_write_is_uncertain_and_never_returns_a_native_uuid(self):
        from orchd import orch_transport
        peer = mock.MagicMock()
        peer.__enter__.return_value = peer
        peer.sendall.side_effect = OSError('interrupted after auth')
        with self.discovery(), mock.patch.object(orch_transport.Endpoint,'connect',return_value=peer):
            with self.assertRaises(UDSWriteUncertain):self.rt.send_orch(self.orch,'once')
        self.assertEqual(peer.sendall.call_count,1)

    def test_primary_uncertain_write_does_not_try_another_endpoint(self):
        from orchd import orch_transport
        with mock.patch.object(self.rt,'send_uds',side_effect=UDSWriteUncertain('partial')), \
                mock.patch.object(orch_transport,'discover') as discover:
            with self.assertRaises(UDSWriteUncertain):self.rt.send_orch(self.orch,'once')
        discover.assert_not_called()

    def test_kernel_peer_pid_attestation_uses_the_connected_socket(self):
        from orchd import orch_transport
        if sys.platform!='darwin' and not sys.platform.startswith('linux'):self.skipTest('kernel peer PID unsupported')
        with socket.socket(socket.AF_UNIX) as peer:
            peer.connect(str(self.physical))
            self.assertEqual(orch_transport._peer_pid(peer),os.getpid())

    def test_worker_context_cannot_use_orch_fallback(self):
        from orchd import orch_transport
        worker = dict(self.orch,kind='worker')
        with mock.patch.object(self.rt,'send_uds') as send:
            with self.assertRaises(orch_transport.TransportUnavailable):self.rt.send_orch(worker,'not an Orch')
        send.assert_not_called()

    def test_worker_sender_identity_is_preserved_and_not_a_receiver_role(self):
        from orchd import orch_transport
        sender = {'ORCHD_TASK_ID':'legitimate-worker','ORCHD_WORKTREE':'/srv/worker'}
        with mock.patch.dict(os.environ,sender), self.discovery(), mock.patch.object(orch_transport,'_peer_pid',return_value=101):
            self.assertTrue(self.rt.orch_listening(self.orch))
            self.assertEqual({k:os.environ[k] for k in sender},sender)

    def test_worker_tagged_receiver_cannot_supply_orch_export(self):
        from orchd import orch_transport
        self.child.env['ORCHD_TASK_ID'] = 'wrong-receiver-role'
        with self.discovery(), self.assertRaises(orch_transport.TransportUnavailable):
            orch_transport.discover(self.rt,self.orch)

    def test_matching_child_and_config_cannot_select_a_foreign_instance_policy(self):
        from orchd import orch_transport
        self.config['env']['ORCHD_CONFIG_DIR'] = '/srv/foreign-config'
        self.child.env['ORCHD_CONFIG_DIR'] = '/srv/foreign-config'
        self.mcp.write_text(json.dumps({'mcpServers':{'orchd':self.config}}))
        with self.discovery(), self.assertRaises(orch_transport.TransportUnavailable):
            orch_transport.discover(self.rt,self.orch)

    def test_zombie_native_process_is_not_a_live_endpoint(self):
        from orchd import orch_transport
        with self.discovery(), mock.patch.object(self.native,'status',return_value='zombie'), \
                self.assertRaises(orch_transport.TransportUnavailable):
            orch_transport.discover(self.rt,self.orch)

    def test_receiver_native_session_tag_is_required_and_rechecked(self):
        from orchd import orch_transport
        with self.discovery():
            endpoint=orch_transport.discover(self.rt,self.orch)
            self.child.env['CLAUDE_CODE_SESSION_ID']='other-session'
            with self.assertRaises(orch_transport.TransportUnavailable):orch_transport.discover(self.rt,self.orch)
            with self.assertRaises(ValueError):endpoint.connect()
            del self.child.env['CLAUDE_CODE_SESSION_ID']
            with self.assertRaises(orch_transport.TransportUnavailable):orch_transport.discover(self.rt,self.orch)

    def test_reviewed_receiver_sdk_need_not_equal_the_new_sender_sdk(self):
        from orchd import orch_transport
        entry=self.config['args'][0]
        self.config['env']['ORCHD_EXECUTABLE']=entry
        self.child.env['ORCHD_EXECUTABLE']=entry
        self.child.env['ORCHD_PROFILE_PREFIX']=str(self.receiver)
        self.mcp.write_text(json.dumps({'mcpServers':{'orchd':self.config}}))
        with mock.patch.dict(os.environ,{'ORCHD_EXECUTABLE':'/srv/new-client/orchd',
                'ORCHD_PROFILE_PREFIX':'/srv/new-client'}), self.discovery(), \
                mock.patch.object(orch_transport,'_peer_pid',return_value=101):
            self.assertTrue(self.rt.orch_listening(self.orch))

    def test_primary_keeps_existing_transport_and_discards_return_uuid(self):
        with mock.patch.object(self.rt,'send_uds',return_value='native-id') as send:
            self.assertIsNone(self.rt.send_orch(self.orch,'primary'))
        send.assert_called_once_with(str(self.logical),'native-session','primary')
