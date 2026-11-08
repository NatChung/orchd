import unittest
from orchd.channels import policy


class AccessTest(unittest.TestCase):
    def test_cross_source_and_forged_operator_denied(self):
        caller = policy.Caller(role='worker', sources=frozenset({'a'}))
        with self.assertRaises(PermissionError):
            policy.require_source(caller, 'b')
        with self.assertRaises(PermissionError):
            policy.require_operator(caller)

    def test_grants_come_from_config_not_payload(self):
        caller=policy.caller_for_identity('orch-a',config={'acl':{'orch-a':['source-a']}})
        policy.require_source(caller,'source-a')
        with self.assertRaises(PermissionError):
            policy.require_source(caller,'source-b')
        with self.assertRaises(PermissionError):
            policy.require_operator(caller)

    def test_unconfigured_identity_has_no_sources(self):
        caller=policy.caller_for_identity('unconfigured',config={'acl':{}})
        with self.assertRaises(PermissionError):
            policy.require_source(caller,'source-a')

    def test_mcp_rejects_payload_identity(self):
        from orchd.channels import mcp
        with self.assertRaises(ValueError):
            mcp.call('channels_message', {'message_id':'x','actor':'operator'}, 'worker', None)

    def test_retention_defaults_and_backup_limit(self):
        self.assertEqual(policy.retention_policies({'retention':{'s':{}}})['s'], {'body_days':90,'attachment_days':90,'backup_days':30})
        self.assertEqual(policy.retention_policies({'retention':{'s':{'body_days':45,'attachment_days':45,'backup_days':7}}})['s']['body_days'],45)
        with self.assertRaises(ValueError):
            policy.retention_policies({'retention':{'s':{'backup_days':31}}})

    def test_mcp_and_cli_never_accept_forged_actor(self):
        from orchd.channels import mcp
        for name, args in [('channels_messages',{'source_id':'a','actor':'operator'}),('channels_sync',{'source_id':'a','request_key':'k','approval':'forged'})]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                mcp.call(name,args,'orch',None)

    def test_channel_receipt_cannot_bypass_acl_through_legacy_mcp(self):
        import tempfile
        from pathlib import Path
        from unittest import mock
        from orchd import mcp_server
        from orchd.channels import service
        with tempfile.TemporaryDirectory() as d:
            svc=service.ChannelService(Path(d)/'db')
            try:
                svc.provision_source({'id':'s','host_id':'h','provider':'slack','tenant_ref':'t','account_ref':'a','adapter_id':'adapter','enabled':True},caller=policy.Caller(role='operator'))
                event={'schema_version':1,'transport_event_key':'e','origin':{'provider':'slack','tenant_ref':'t','conversation_ref':'c','message_id':'m'},'revision_key':'initial','conversation':{'kind':'channel','thread_key':''},'sender_ref':'sender','source_time':'2026-01-01T00:00:00Z','content':{'status':'complete','text':'synthetic','attachments':[]}}
                receipt=svc.receive('s',event,caller=policy.Caller(role='adapter',sources=frozenset({'s'}),identity='adapter'))
                with mock.patch('orchd.channels.policy.caller_for_identity', return_value=policy.Caller(role='orch',sources=frozenset({'other'}),identity='orch')):
                    with self.assertRaises(PermissionError):
                        mcp_server.intake_call('intake_show',{'receipt_id':receipt['receipt_id']},'orch',svc.con)
            finally:
                svc.close()

    def test_cli_and_mcp_enforce_same_source_acl_without_network(self):
        import contextlib
        import io
        import os
        import tempfile
        from pathlib import Path
        from unittest import mock
        from orchd import cli, mcp_server, store
        from orchd.channels import service
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'orchd.db'
            svc=service.ChannelService(path)
            try:
                svc.provision_source({'id':'s','host_id':'h','provider':'slack','tenant_ref':'t','account_ref':'a','adapter_id':'adapter','enabled':True},caller=policy.Caller(role='operator'))
                event={'schema_version':1,'transport_event_key':'e','origin':{'provider':'slack','tenant_ref':'t','conversation_ref':'c','message_id':'m'},'revision_key':'initial','conversation':{'kind':'channel','thread_key':''},'sender_ref':'sender','source_time':'2026-01-01T00:00:00Z','content':{'status':'complete','text':'synthetic private body','attachments':[]}}
                receipt=svc.receive('s',event,caller=policy.Caller(role='adapter',sources=frozenset({'s'}),identity='adapter'))
            finally:
                svc.close()
            good=policy.Caller(role='orch',sources=frozenset({'s'}),identity='orch')
            bad=policy.Caller(role='orch',sources=frozenset({'other'}),identity='orch')
            con=store.connect(path)
            try:
                with mock.patch('socket.socket',side_effect=AssertionError('network forbidden')), mock.patch('orchd.channels.policy.caller_for_identity',return_value=good):
                    result=mcp_server.call('channels_message',{'message_id':receipt['message_id']},'orch',con,None)
                    self.assertEqual(result['content']['text'],'synthetic private body')
                    out=io.StringIO()
                    with mock.patch.dict(os.environ,{'ORCHD_HOME':d,'ORCHD_ORCH_ID':'orch','ORCHD_WORKTREE':''}), contextlib.redirect_stdout(out):
                        self.assertEqual(cli.main(['channels','message',receipt['message_id'],'--json']),0)
                    self.assertIn('synthetic private body',out.getvalue())
                with mock.patch('orchd.channels.policy.caller_for_identity',return_value=bad):
                    with self.assertRaises(PermissionError):
                        mcp_server.call('channels_message',{'message_id':receipt['message_id']},'orch',con,None)
                    with mock.patch.dict(os.environ,{'ORCHD_HOME':d,'ORCHD_ORCH_ID':'orch','ORCHD_WORKTREE':''}), contextlib.redirect_stderr(io.StringIO()):
                        self.assertEqual(cli.main(['channels','message',receipt['message_id'],'--json']),1)
            finally:
                con.close()

    def test_worker_grants_are_task_scoped(self):
        import tempfile
        from pathlib import Path
        from unittest import mock
        from orchd import store
        with tempfile.TemporaryDirectory() as d:
            con=store.connect(Path(d)/'db')
            try:
                con.execute("INSERT INTO tasks(id,repo,repo_path,title,instructions,done_when,orch_thread,codex_bin,worktree,status,created_at,updated_at) VALUES('task','demo','repo','title','instructions','done','owner','codex',?,'running',0,0)",(str(Path(d)/'worktree'),))
                with mock.patch('orchd.channels.policy.caller_for_identity', return_value=policy.Caller(role='orch',sources=frozenset({'s'}),identity='worker:task')):
                    caller=policy.caller_for_worker(con,str(Path(d)/'worktree'))
                self.assertEqual(caller.owner_orch,'owner')
                policy.require_source(caller,'s')
                with self.assertRaises(PermissionError):
                    policy.caller_for_worker(con,str(Path(d)/'other'))
            finally:
                con.close()
