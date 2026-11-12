from tests.channel_outbound_helpers import OutboundCase


class GrantTest(OutboundCase):
    def grant(self, **changes):
        fields = dict(source_id='s', reason='Allow replies', provenance='Operator decision')
        fields.update(changes)
        return self.svc.create_grant(**fields, caller=self.op)

    def auto(self, draft):
        return self.svc.approve(draft['draft_id'], draft['version'], draft['payload_hash'], caller=self.worker)

    def test_matching_grant_approves_and_audits_delivery(self):
        grant = self.grant()
        draft = self.draft()
        approved = self.auto(draft)
        claim = self.svc.claim_outbound('s', caller=self.adapter)[0]
        self.svc.record_outcome(claim['id'], 1, 'provider_accepted', 'receipt', caller=self.adapter)
        audit = self.svc.grant_audit('s', caller=self.worker)
        self.assertEqual(audit[0]['grant_id'], grant['grant_id'])
        self.assertEqual(audit[0]['payload_hash'], draft['payload_hash'])
        self.assertEqual(audit[0]['state'], 'provider_accepted')
        self.assertEqual(audit[0]['delivery_id'], approved['delivery_id'])

    def test_revocation_after_approval_blocks_claim_and_restores_exact_approval(self):
        grant = self.grant()
        draft = self.draft()
        self.auto(draft)
        self.svc.revoke_grant(grant['grant_id'], 'Stop', caller=self.op)
        self.assertEqual(self.svc.claim_outbound('s', caller=self.adapter), [])
        self.approve(draft)
        self.assertEqual(len(self.svc.claim_outbound('s', caller=self.adapter)), 1)

    def test_nonmatching_grant_requires_exact_approval(self):
        self.grant(target='another-conversation')
        draft = self.draft()
        with self.assertRaises(ValueError):
            self.auto(draft)
        self.approve(draft)
        self.assertEqual(len(self.svc.claim_outbound('s',caller=self.adapter)),1)

    def test_revoked_grant_requires_exact_approval(self):
        grant = self.grant()
        self.svc.revoke_grant(grant['grant_id'],'Stop',caller=self.op)
        self.assertEqual(self.svc.list_grants('s',caller=self.op)[0]['state'],'revoked')
        draft = self.draft()
        with self.assertRaises(ValueError):
            self.auto(draft)
        self.approve(draft)

    def test_expired_grant_requires_exact_approval(self):
        from datetime import timedelta
        self.grant(expires_at=self.now.timestamp()+10)
        draft = self.draft()
        self.now += timedelta(seconds=10)
        with self.assertRaises(ValueError):
            self.auto(draft)
        self.approve(draft)

    def test_non_operator_cannot_create_grant(self):
        from orchd.channels.policy import Caller
        for caller in (self.worker,self.adapter,Caller(role='worker',sources=frozenset({'s'})),Caller(role='peer',sources=frozenset({'s'}))):
            with self.subTest(role=caller.role), self.assertRaises(PermissionError):
                self.svc.create_grant('s','Reason','Evidence',caller=caller)
        self.assertEqual(self.svc.list_grants('s',caller=self.op),[])

    def test_uncertain_grant_delivery_is_never_retried(self):
        self.grant()
        draft = self.draft()
        self.auto(draft)
        claim = self.svc.claim_outbound('s',caller=self.adapter)[0]
        self.svc.record_outcome(claim['id'],1,'uncertain',caller=self.adapter)
        self.assertEqual(self.svc.claim_outbound('s',caller=self.adapter),[])
        self.assertEqual(self.svc.grant_audit('s',caller=self.worker)[0]['state'],'uncertain')

    def test_worker_or_third_party_cannot_supply_auto_approved_draft(self):
        from orchd.channels.policy import Caller
        self.grant()
        payload = dict(sender='s',target='c',body='Synthetic',expires_at=self.now.timestamp()+100)
        for role in ('worker','adapter','peer'):
            caller = Caller(role=role,sources=frozenset({'s'}),identity=self.owner,owner_orch=self.owner)
            if role != 'worker':
                with self.assertRaises(PermissionError):
                    self.svc.draft_reply(self.mid,payload,caller=caller)
            else:
                draft = self.svc.draft_reply(self.mid,payload,caller=caller)
                with self.assertRaises(ValueError):
                    self.auto(draft)
                self.approve(draft)

    def test_grant_does_not_enable_outbound_or_receive_only_route(self):
        self.grant()
        self.auto(self.draft())
        self.enable(False)
        with self.assertRaises(PermissionError):
            self.svc.claim_outbound('s',caller=self.adapter)
        self.enable()
        self.routes(mode='receive_only')
        self.assertEqual(self.svc.claim_outbound('s',caller=self.adapter),[])

    def test_expired_grant_blocks_already_approved_claim(self):
        from datetime import timedelta
        self.grant(expires_at=self.now.timestamp()+10)
        self.auto(self.draft())
        self.now += timedelta(seconds=10)
        self.assertEqual(self.svc.claim_outbound('s',caller=self.adapter),[])

    def test_cli_grant_parser_and_dispatch(self):
        import contextlib, io, json, os
        from unittest.mock import patch
        from orchd.cli import build_parser, _home_policy
        from orchd.channels import cli
        parser = build_parser()
        with patch.dict(os.environ,{},clear=True), patch.object(cli.store,'home',return_value=self.svc.channel_home), patch.object(cli.policy,'retention_policies',return_value={}), patch.object(cli.service,'ChannelService',return_value=self.svc), patch.object(self.svc,'close'):
            for command, content in [(['create','--file','-'],json.dumps(dict(source_id='s',reason='Allow',provenance='Operator original'))),(['list','--source','s'],''),(['audit','--source','s'],'')]:
                args = parser.parse_args(['channels','grant',*command,'--json'])
                self.assertEqual(_home_policy(args),'skip')
                with contextlib.redirect_stdout(io.StringIO()) as output, patch('sys.stdin',io.StringIO(content)):
                    self.assertEqual(cli.run(args),0)
                result=json.loads(output.getvalue())
                if command[0] == 'audit':
                    self.assertEqual(result,[])
                else:
                    self.assertTrue(result)
            grant = self.svc.list_grants('s',caller=self.op)[0]
            args=parser.parse_args(['channels','grant','revoke',grant['grant_id'],'--reason','Stop','--json'])
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.run(args),0)


    def test_mcp_approve_uses_same_grant_gate_and_cannot_create_grant(self):
        from unittest.mock import patch
        from orchd.channels import mcp
        self.grant()
        draft = self.draft()
        with patch.object(mcp.policy,'caller_for_identity',return_value=self.worker), patch.object(mcp.service,'ChannelService',return_value=self.svc), patch.object(self.svc,'close'):
            result=mcp.call('channels_approve',{k:draft[k] for k in ('draft_id','version','payload_hash')},self.owner,self.svc.con)
        self.assertEqual(result['status'],'approved')
        with self.assertRaises(ValueError):
            mcp.call('channels_grant_create',{'source_id':'s'},self.owner,self.svc.con)

    def test_cli_worker_environment_cannot_create_grant(self):
        import contextlib, io, os
        from unittest.mock import patch
        from orchd.cli import build_parser
        from orchd.channels import cli, policy
        args=build_parser().parse_args(['channels','grant','create'])
        worker=policy.Caller(role='worker',sources=frozenset({'s'}),identity='worker',owner_orch=self.owner)
        with patch.dict(os.environ,{'ORCHD_WORKTREE':'synthetic-worktree'}), patch.object(cli.store,'home',return_value=self.svc.channel_home), patch.object(cli.policy,'retention_policies',return_value={}), patch.object(cli.service,'ChannelService',return_value=self.svc), patch.object(self.svc,'close'), patch.object(cli.policy,'caller_for_worker',return_value=worker), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.run(args),1)
        self.assertEqual(self.svc.list_grants('s',caller=self.op),[])

    def test_legacy_draft_without_author_evidence_requires_exact_approval(self):
        self.grant()
        draft=self.draft()
        self.svc.con.execute('DELETE FROM channel_draft_authors WHERE draft_id=?',(draft['draft_id'],))
        with self.assertRaises(ValueError):
            self.auto(draft)
        self.approve(draft)

    def test_route_scope_and_sender_and_payload_binding_remain_enforced(self):
        self.grant(route_id='r-s',target='c')
        draft=self.draft()
        self.auto(draft)
        self.svc.con.execute("UPDATE intake_approvals SET sender='other' WHERE draft_id=?",(draft['draft_id'],))
        self.assertEqual(self.svc.claim_outbound('s',caller=self.adapter),[])
        with self.assertRaises(PermissionError):
            self.draft(sender='other')
