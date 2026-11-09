import io
import unittest
from orchd.channels import setup


class SetupTests(unittest.TestCase):
    def test_dry_run_uses_injected_io_and_does_not_request_tokens(self):
        answers = iter(['slack', 'demo', 'T123', 'C123', 'orch', 'n'])
        output = io.StringIO()
        result = setup.run(input_fn=lambda _: next(answers), secret_fn=lambda _: self.fail('secret requested'), output=output, dry_run=True)
        self.assertEqual(result['source']['id'], 'demo')
        self.assertEqual(result['route']['conversation_ref'], 'C123')
        self.assertIn('dry-run', output.getvalue())

    def test_setup_provisions_through_command_executor_with_secret_input(self):
        answers = iter(['line', 'demo', 'tenant', 'conversation', 'orch', 'n'])
        commands = []
        setup.run(input_fn=lambda _: next(answers), secret_fn=lambda _: 'synthetic', output=io.StringIO(), execute=lambda action, payload: commands.append((action, payload)))
        self.assertEqual([action for action, _ in commands], ['check', 'secret', 'source', 'route', 'enable', 'doctor'])
        self.assertEqual(commands[1][1][-1], b'synthetic')

    def test_setup_command_parses_dry_run(self):
        from orchd.cli import build_parser
        args = build_parser().parse_args(['channels', 'setup', '--dry-run'])
        self.assertTrue(args.dry_run)

class MacStoreTests(unittest.TestCase):
    def test_write_keeps_secret_out_of_process_arguments(self):
        from unittest import mock
        from orchd.channels import secret_store
        runner = mock.Mock(return_value=mock.Mock(returncode=0, stdout=b'', stderr=b''))
        provider = secret_store.MacOSSecretStore(runner=runner)
        provider.put('slack', 'demo', 'os-store:demo/app', b'synthetic-token')
        args, kwargs = runner.call_args
        self.assertNotIn('synthetic-token', repr(args))
        self.assertNotIn('synthetic-token', repr(kwargs.get('args', [])))
        self.assertIn(b'add-generic-password', kwargs['input'])

    def test_lookup_decodes_stored_bytes_and_scopes_account(self):
        from unittest import mock
        from orchd.channels import secret_store
        runner = mock.Mock(return_value=mock.Mock(returncode=0, stdout=b'c3ludGhldGlj\n', stderr=b''))
        provider = secret_store.MacOSSecretStore(runner=runner)
        self.assertEqual(provider.get('slack', 'demo', 'os-store:demo/app'), b'synthetic')
        self.assertIn('orchd/channel/slack/demo/os-store:demo/app', runner.call_args.args[0])

class ServiceTests(unittest.TestCase):
    def test_windows_install_status_stop_and_uninstall(self):
        import tempfile
        from pathlib import Path
        from unittest import mock
        from orchd.channels import system_service
        with tempfile.TemporaryDirectory() as tmp:
            runner = mock.Mock(return_value=mock.Mock(returncode=0, stdout='Ready', stderr=''))
            system_service.command('install', Path(tmp), platform='win32', runner=runner, executable='C:/orchd.exe')
            self.assertEqual([c.args[0][1] for c in runner.call_args_list], ['/Create', '/Run'])
            state = system_service.command('status', Path(tmp), platform='win32', runner=runner)
            self.assertTrue(state['registered'])
            system_service.command('stop', Path(tmp), platform='win32', runner=runner)
            self.assertEqual(runner.call_args.args[0][1], '/End')
            system_service.command('uninstall', Path(tmp), platform='win32', runner=runner)
            self.assertEqual(runner.call_args.args[0][1], '/Delete')

    def test_macos_install_and_remove_use_launchctl(self):
        import tempfile
        from pathlib import Path
        from unittest import mock
        from orchd.channels import system_service
        with tempfile.TemporaryDirectory() as tmp:
            runner = mock.Mock(return_value=mock.Mock(returncode=0, stdout='', stderr=''))
            system_service.command('install', Path(tmp)/'channel', platform='darwin', runner=runner, executable='/bin/orchd', user_home=Path(tmp), uid=501)
            self.assertEqual(runner.call_args.args[0][1], 'bootstrap')
            self.assertTrue((Path(tmp)/'Library/LaunchAgents/com.orchd.channels.plist').is_file())
            system_service.command('uninstall', Path(tmp)/'channel', platform='darwin', runner=runner, user_home=Path(tmp), uid=501)
            self.assertEqual(runner.call_args.args[0][1], 'bootout')

    def test_service_cli_and_serve_log_options_parse(self):
        from orchd.cli import build_parser
        parser = build_parser()
        self.assertEqual(parser.parse_args(['channels', 'service', 'status']).service_action, 'status')
        self.assertTrue(parser.parse_args(['channels', 'serve', '--service-log']).service_log)

    def test_running_lock_rejects_second_serve(self):
        import tempfile
        from pathlib import Path
        from unittest import mock
        from orchd import file_lock
        from orchd.channels import serve
        with tempfile.TemporaryDirectory() as tmp:
            svc = mock.Mock(channel_home=Path(tmp))
            with (Path(tmp)/'serve.lock').open('a+b') as handle:
                file_lock.try_lock(handle)
                with self.assertRaises(BlockingIOError):
                    serve.run(svc)

    def test_failed_service_query_is_not_reported_as_unregistered(self):
        import tempfile
        from unittest import mock
        from orchd.channels import system_service
        runner = mock.Mock(return_value=mock.Mock(returncode=1, stdout='', stderr='Access denied'))
        with tempfile.TemporaryDirectory() as home:
            with self.assertRaisesRegex(OSError, 'query failed'):
                system_service.command('status', home, platform='win32', runner=runner)

    def test_cli_dry_run_does_not_open_database(self):
        from unittest import mock
        from orchd.channels import cli
        from orchd.cli import build_parser
        args = build_parser().parse_args(['channels', 'setup', '--dry-run'])
        with mock.patch('orchd.channels.setup.run') as wizard, mock.patch.object(cli.service, 'ChannelService', side_effect=AssertionError('database opened')):
            self.assertEqual(cli.run(args), 0)
            wizard.assert_called_once_with(dry_run=True)

    def test_service_preserves_explicit_homes_in_launch_configuration(self):
        import tempfile, plistlib
        from pathlib import Path
        from unittest import mock
        from orchd.channels import system_service
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)/'channel home'
            database = Path(tmp)/'state home'
            runner = mock.Mock(return_value=mock.Mock(returncode=0, stdout='', stderr=''))
            system_service.command('install', home, platform='darwin', runner=runner, executable='/bin/orchd', user_home=Path(tmp), uid=501, db_home=database)
            with (Path(tmp)/'Library/LaunchAgents/com.orchd.channels.plist').open('rb') as f:
                args = plistlib.load(f)['ProgramArguments']
            self.assertIn(str(home), args)
            self.assertIn(str(database), args)

class MacOperationsTests(unittest.TestCase):
    def test_delete_and_list_are_scoped(self):
        from unittest import mock
        from orchd.channels import secret_store
        dump = b'"acct"<blob>="orchd/channel/slack/demo/os-store:demo/app"\n"acct"<blob>="orchd/channel/slack/other/os-store:other/api"\n'
        runner = mock.Mock(return_value=mock.Mock(returncode=0, stdout=dump, stderr=b''))
        provider = secret_store.MacOSSecretStore(runner=runner)
        self.assertEqual(provider.list('slack', 'demo'), ['os-store:demo/app'])
        provider.delete('slack', 'demo', 'os-store:demo/app')
        self.assertIn('orchd/channel/slack/demo/os-store:demo/app', runner.call_args.args[0])

    def test_keychain_failure_is_redacted(self):
        from unittest import mock
        from orchd.channels import secret_store
        runner = mock.Mock(return_value=mock.Mock(returncode=1, stdout=b'', stderr=b'secret text'))
        provider = secret_store.MacOSSecretStore(runner=runner)
        with self.assertRaisesRegex(OSError, '^Keychain operation failed$'):
            provider.put('slack', 'demo', 'os-store:demo/app', b'synthetic')

    def test_macos_status_and_stop(self):
        import tempfile
        from pathlib import Path
        from unittest import mock
        from orchd.channels import system_service
        runner = mock.Mock(return_value=mock.Mock(returncode=0, stdout='', stderr=''))
        with tempfile.TemporaryDirectory() as tmp:
            state = system_service.command('status', Path(tmp), platform='darwin', runner=runner, uid=501)
            self.assertEqual((state['registered'], state['running'], state['last_received']), (True, False, None))
            system_service.command('stop', Path(tmp), platform='darwin', runner=runner, uid=501)
            self.assertEqual(runner.call_args.args[0][1:3], ['kill', 'SIGTERM'])

from channel_helpers import ChannelCase

class SetupIntegrationTests(ChannelCase):
    def test_setup_command_provisions_enabled_source_and_route(self):
        from unittest import mock
        import contextlib
        from orchd.channels import cli, setup, management
        from orchd.cli import build_parser
        answers = iter(['line', 'demo', 'tenant', 'conversation', 'orch', 'n'])
        facade = mock.Mock(wraps=self.svc)
        facade.close = mock.Mock()
        real_run = setup.run
        provider = mock.Mock()
        def run_wizard(**kwargs):
            return real_run(input_fn=lambda _: next(answers), secret_fn=lambda _: 'fake-token', output=io.StringIO(), **kwargs)
        with mock.patch.object(cli.service, 'ChannelService', return_value=facade), mock.patch('orchd.channels.secret_store.default_store', return_value=provider), mock.patch.object(setup, 'run', side_effect=run_wizard), mock.patch.dict('os.environ', {'ORCHD_ORCH_ID':'', 'CODEX_THREAD_ID':'', 'ORCHD_WORKTREE':''}), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.run(build_parser().parse_args(['channels', 'setup'])), 0)
        sources = management.sources(self.svc, self.op)
        self.assertEqual([(s['id'], s['enabled']) for s in sources], [('demo', 1)])
        self.assertEqual(self.svc.routes(caller=self.op)[0]['conversation_ref'], 'conversation')
        provider.put.assert_called_once_with('line', 'demo', 'os-store:demo/signing', b'fake-token')

    def test_existing_source_does_not_overwrite_credentials(self):
        from unittest import mock
        import contextlib
        from orchd.channels import cli, setup
        from orchd.cli import build_parser
        self.source('demo', 'line')
        answers = iter(['line', 'demo', 'tenant', 'conversation', 'orch', 'n'])
        facade = mock.Mock(wraps=self.svc); facade.close = mock.Mock()
        real_run = setup.run
        provider = mock.Mock()
        def run_wizard(**kwargs):
            return real_run(input_fn=lambda _: next(answers), secret_fn=lambda _: 'fake-token', output=io.StringIO(), **kwargs)
        with mock.patch.object(cli.service, 'ChannelService', return_value=facade), mock.patch('orchd.channels.secret_store.default_store', return_value=provider), mock.patch.object(setup, 'run', side_effect=run_wizard), mock.patch.dict('os.environ', {'ORCHD_ORCH_ID':'', 'CODEX_THREAD_ID':'', 'ORCHD_WORKTREE':''}), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.run(build_parser().parse_args(['channels', 'setup'])), 1)
        provider.put.assert_not_called()
