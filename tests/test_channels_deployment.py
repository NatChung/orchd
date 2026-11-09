import unittest
from unittest import mock
from orchd.channels import secret_store


class WindowsStoreTests(unittest.TestCase):
    def test_source_scoped_round_trip_and_listing(self):
        backend = mock.Mock()
        backend.read.return_value = b'synthetic'
        backend.list.return_value = ['orchd/channel/adapter/source/os-store:demo/key']
        store = secret_store.WindowsSecretStore(backend=backend)
        store.put('adapter', 'source', 'os-store:demo/key', b'synthetic')
        self.assertEqual(store.get('adapter', 'source', 'os-store:demo/key'), b'synthetic')
        self.assertEqual(store.list('adapter', 'source'), ['os-store:demo/key'])
        store.delete('adapter', 'source', 'os-store:demo/key')
        backend.delete.assert_called_once_with('orchd/channel/adapter/source/os-store:demo/key')

    def test_invalid_components_are_rejected(self):
        store = secret_store.WindowsSecretStore(backend=mock.Mock())
        with self.assertRaises(ValueError):
            store.put('../adapter', 'source', 'os-store:demo/key', b'synthetic')
        with self.assertRaises(ValueError):
            store.put('adapter', 'source', 'os-store:../key', b'synthetic')

    def test_unsupported_os_fails_explicitly(self):
        with mock.patch.object(secret_store.os, 'name', 'posix'):
            with self.assertRaisesRegex(OSError, 'Windows'):
                secret_store.WindowsSecretStore()

class ManagementTests(unittest.TestCase):
    def test_route_add_and_remove_preserve_other_routes(self):
        from orchd.channels import management
        svc = mock.Mock()
        svc.routes.return_value = [dict(id='old', source_id='s', conversation_ref='c', owner_orch='o', sender_scope_json='[]', mode='receive_only', authority_epoch=1, project_ref=None, repo_ref=None, responder_source_id=None)]
        op = object()
        new = dict(id='new', source_id='s', conversation_ref='d', owner_orch='o')
        management.change_route(svc, op, 'add', new)
        self.assertEqual([r['id'] for r in svc.apply_routes.call_args.args[0]], ['old', 'new'])
        management.change_route(svc, op, 'remove', 'old')
        self.assertEqual(svc.apply_routes.call_args.args[0], [])
        with self.assertRaises(ValueError): management.change_route(svc, op, 'remove', 'missing')

    def test_serve_stops_and_closes_resources(self):
        from orchd.channels import serve
        stop = mock.Mock()
        stop.is_set.return_value = True
        http = mock.Mock()
        receiver = mock.Mock()
        serve.run_loop(http, [receiver], stop)
        http.server_close.assert_called_once()

class CommandParserTests(unittest.TestCase):
    def test_secret_never_accepts_value_argument(self):
        from orchd.cli import build_parser
        parser = build_parser()
        args = parser.parse_args(['channels', 'secret', 'put', '--adapter', 'a', '--source', 's', '--ref', 'os-store:x'])
        self.assertEqual(args.secret_action, 'put')
        with self.assertRaises(SystemExit):
            parser.parse_args(['channels', 'secret', 'put', '--adapter', 'a', '--source', 's', '--ref', 'os-store:x', '--value', 'synthetic'])

    def test_source_and_route_commands_parse(self):
        from orchd.cli import build_parser
        parser = build_parser()
        self.assertEqual(parser.parse_args(['channels', 'source', 'enable', 's']).source_action, 'enable')
        self.assertEqual(parser.parse_args(['channels', 'route', 'remove', 'r']).route_action, 'remove')
        self.assertEqual(parser.parse_args(['channels', 'serve']).host, '127.0.0.1')

from channel_helpers import ChannelCase

class ServeSmokeTests(ChannelCase):
    def test_synthetic_slack_and_line_generate_receipts(self):
        import base64, hashlib, hmac, json, socket, threading, time
        from urllib.request import Request, urlopen
        from orchd.channels import serve
        from orchd.channels.secret_store import FakeSecretStore
        from test_channels_slack import envelope
        from test_channels_line import message
        self.source('slack', 'slack')
        self.source('line', 'line')
        self.svc.apply_routes([dict(id='rs', source_id='slack', conversation_ref='c', owner_orch='orch'), dict(id='rl', source_id='line', conversation_ref='c', owner_orch='orch')], caller=self.op)
        home = self.svc.channel_home
        home.mkdir(parents=True)
        (home / 'channels.toml').write_text('[channels]\nschema_version = 1\n[channels.slack.slack]\nactivation = "1767225600"\n', encoding='utf-8')
        secrets = FakeSecretStore({('slack','slack','os-store:slack/app'):b'fake', ('slack','slack','os-store:slack/api'):b'fake', ('line','line','os-store:line/signing'):b'fake'})
        stop = threading.Event()
        errors = []
        class FakeTransport:
            def auth_test(self, credential): return {'ok': True, 'team_id': 't', 'user_id': 'account'}
            def connect(self, credentials, callback): callback(envelope())
            def ack(self, key): pass
            def close(self): pass
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
        def request():
            try:
                raw = json.dumps({'events':[message()]}).encode()
                signature = base64.b64encode(hmac.new(b'fake',raw,hashlib.sha256).digest()).decode()
                for attempt in range(100):
                    try:
                        with urlopen(Request(f'http://127.0.0.1:{port}/webhook', data=raw, headers={'X-Line-Signature':signature}), timeout=2) as response:
                            self.assertEqual(response.status, 200)
                        break
                    except OSError:
                        time.sleep(.05)
                else: raise AssertionError('server did not start')
                time.sleep(.5)
            except Exception as error: errors.append(error)
            finally: stop.set()
        thread = threading.Thread(target=request); thread.start()
        real_adapter = serve.SlackAdapter
        with mock.patch('orchd.channels.adapters.slack.SlackTransport', FakeTransport), mock.patch.object(serve, 'SlackAdapter', wraps=real_adapter) as factory:
            serve.run(self.svc, port=port, stop=stop, provider=secrets)
        self.assertEqual(factory.call_args.kwargs, dict(conversations=('c',), self_identities=(), activation='1767225600'))
        thread.join()
        if errors: raise errors[0]
        rows = self.svc.con.execute('SELECT intake_receipt_id FROM channel_messages ORDER BY intake_receipt_id').fetchall()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row[0] for row in rows))
        print('synthetic smoke receipts:', ', '.join(row[0] for row in rows))

class SourceManagementTests(ChannelCase):
    def test_provision_list_enable_disable(self):
        from orchd.channels import management
        source = dict(id='one', host_id='h', provider='line', account_ref='a', adapter_id='a', credential_refs={'signing':'os-store:line/key'})
        self.svc.provision_source(source, caller=self.op)
        self.assertEqual(management.sources(self.svc, self.op)[0]['enabled'], 0)
        management.set_enabled(self.svc, self.op, 'one', True)
        self.assertEqual(management.sources(self.svc, self.op)[0]['enabled'], 1)
        management.set_enabled(self.svc, self.op, 'one', False)
        self.assertEqual(management.sources(self.svc, self.op)[0]['enabled'], 0)
        with self.assertRaises(ValueError): management.set_enabled(self.svc, self.op, 'missing', True)

class CliExecutionTests(ChannelCase):
    def test_source_and_route_cli_dispatch(self):
        import contextlib, io, json
        from orchd.cli import build_parser
        from orchd.channels import cli
        facade = mock.Mock(wraps=self.svc)
        facade.close = mock.Mock()
        parser = build_parser()
        source = dict(id='s', host_id='h', provider='line', account_ref='a', adapter_id='a', credential_refs={'signing':'os-store:line/key'})
        def invoke(command, data=None):
            with mock.patch.object(cli.service, 'ChannelService', return_value=facade), mock.patch.dict('os.environ', {'ORCHD_ORCH_ID':'', 'CODEX_THREAD_ID':'', 'ORCHD_WORKTREE':''}), mock.patch('sys.stdin', io.StringIO(json.dumps(data))), contextlib.redirect_stdout(io.StringIO()):
                return cli.run(parser.parse_args(['channels'] + command))
        self.assertEqual(invoke(['source','provision'], source), 0)
        self.assertEqual(invoke(['source','enable','s']), 0)
        self.assertEqual(invoke(['source','list']), 0)
        route = dict(id='r', source_id='s', conversation_ref='c', owner_orch='orch')
        self.assertEqual(invoke(['route','add'], route), 0)
        self.assertEqual(invoke(['route','list']), 0)
        self.assertEqual(invoke(['route','remove','r']), 0)
        self.assertEqual(invoke(['source','disable','s']), 0)

class SlackServeConfigurationTests(unittest.TestCase):
    def test_failure_description_excludes_sensitive_exception_details(self):
        from orchd.channels.serve import failure_description
        from orchd.channels.adapters.slack import SlackError
        from urllib.error import URLError
        self.assertEqual(failure_description(URLError('secret')), 'URLError')
        self.assertEqual(failure_description(SlackError('missing_scope')), 'SlackError/missing_scope')
        self.assertEqual(failure_description(SlackError('secret')), 'SlackError')

    def test_source_settings_and_routes(self):
        from orchd.channels.serve import slack_options
        layout = {'slack': {'s': {'activation': '1767225600', 'self_identities': ['extra']}}}
        routes = [dict(source_id='s', conversation_ref='c'), dict(source_id='other', conversation_ref='d')]
        self.assertEqual(slack_options(layout, 's', routes), dict(conversations=('c',), self_identities=('extra',), activation='1767225600'))
        with self.assertRaises(ValueError): slack_options({}, 's', routes)
        with self.assertRaises(ValueError): slack_options(layout, 's', [dict(source_id='s', conversation_ref='*')])
