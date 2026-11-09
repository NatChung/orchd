import unittest
from unittest.mock import patch
from orchd.channels.serve import slack_outbound


class SlackOutboundServeTest(unittest.TestCase):
    def test_explicit_source_opt_in(self):
        source = {'id': 'slack-main'}
        self.assertIsNone(slack_outbound({}, source, None, None, None))
        self.assertIsNone(slack_outbound({'slack': {'other': {'outbound_enabled': True}}}, source, None, None, None))
        with patch('orchd.channels.serve.OutboundAdapter') as adapter:
            result = slack_outbound({'slack': {'slack-main': {'outbound_enabled': True}}}, source, 'session', 'caller', 'secrets')
            self.assertIs(result, adapter.return_value)
            self.assertEqual(adapter.call_args.args[:4], ('session', 'slack-main', 'caller', 'secrets'))
        with self.assertRaises(ValueError):
            slack_outbound({'slack': {'slack-main': {'outbound_enabled': 'true'}}}, source, None, None, None)

    def test_outbound_failure_is_sanitized_in_serve_error(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import Mock
        from orchd.channels import serve

        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, 'channels.toml').write_text(
                '[channels]\nschema_version = 1\n[channels.slack.slack-main]\n'
                'activation = "1"\noutbound_enabled = true\n', encoding='utf-8')
            svc = Mock(channel_home=tmp)
            svc.routes.return_value = []
            svc.session_factory.return_value = Mock()
            row = {'id': 'slack-main', 'enabled': True, 'provider': 'slack', 'adapter_id': 'slack-main'}
            secret = 'token-secret https://example.invalid/private message-content'
            with patch.object(serve.management, 'sources', return_value=[row]), \
                 patch.object(serve, 'SlackAdapter') as adapter, \
                 patch.object(serve, 'AdapterRunner') as runner, \
                 patch.object(serve, 'HTTPServer'), \
                 patch.object(serve, 'run_loop', side_effect=lambda http, receivers, stop: stop.wait(5)):
                adapter.return_value.connect.return_value = {}
                runner.return_value.run.side_effect = RuntimeError(secret)
                with self.assertRaises(RuntimeError) as caught:
                    serve._run(svc, provider=Mock())
            self.assertEqual(str(caught.exception), 'Slack adapter failed: slack-main (RuntimeError)')
            for sensitive in ('token-secret', 'https://example.invalid/private', 'message-content'):
                self.assertNotIn(sensitive, str(caught.exception))
            self.assertIsNotNone(runner.call_args.kwargs['outbound'])
