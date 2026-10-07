import os
import unittest
from unittest.mock import patch

from orchd import app_worker, core, mcp_server, store
from orchd.runtime import Runtime
from tests.app_support import AppBase
from tests.test_orchd import FakeRuntime


class AppViewerTest(AppBase):
    def test_busy_and_idle_open_only_stored_task_remote_wrapper(self):
        self.rt.open_app_viewer=lambda db,task:setattr(self,'opened',(db,dict(task)))
        for state in ('active','idle'):
            with patch.object(app_worker,'health',return_value=state):
                text=core.view(self.con,self.rt,self.id)
            self.assertIn('Ghostty',text)
            self.assertEqual(self.opened[1]['session_id'],'thread')
            self.assertEqual(self.opened[1]['generation'],self.gen)
        rt=Runtime()
        with patch.object(rt,'run') as run,patch('orchd.runtime.worker_env',return_value={}):
            rt.open_app_viewer(str(self.db),self.task)
        cmd=run.call_args.args[0]
        self.assertNotIn(self.task['endpoint'],cmd)
        self.assertEqual(cmd[-2:],[self.id,self.gen])

    def test_viewer_ownership_keeps_idle_queue_until_release(self):
        self.queue();self.idle()
        with patch.object(self.supervisor,'viewers',return_value=['viewer']):
            self.assertEqual(self.supervisor.flush()['status'],'queued')
        self.assertEqual(self.supervisor.rpc.calls,[])
        self.assertEqual(self.supervisor.flush()['delivered'],1)

    def test_untrusted_socket_directory_is_refused(self):
        self.root.chmod(0o755)
        with self.assertRaises(ValueError):app_worker.validate(self.task)

    def test_default_dispatch_and_migration_remain_exec(self):
        task=core.dispatch(self.con,self.rt,orch_thread='thread-A',repo='demo',title='T',instructions='i',
                           done_when='d',model_reason='r',task_type='code')
        self.assertEqual(task['backend'],'exec')
        self.assertIsNone(task['generation'])
        self.assertFalse(app_worker.enabled(task))
        tool=next(t for t in mcp_server.TOOLS if t['name']=='dispatch')
        self.assertEqual(tool['inputSchema']['properties']['backend']['default'],'exec')

    def test_definite_rpc_rejection_keeps_pending_but_is_retryable(self):
        from orchd.app_transport import RPCError
        self.queue();self.idle();self.supervisor.rpc.fail=RPCError('busy')
        self.assertEqual(self.supervisor.flush()['status'],'failed')
        self.assertEqual(store.pending_answer_count(self.con,self.id),1)
        self.supervisor.rpc.fail=None
        self.assertEqual(self.supervisor.flush()['delivered'],1)

    def test_reconnect_reconciles_matching_client_id_without_resending(self):
        import json
        import time
        mid=self.queue();self.idle()
        self.con.execute('INSERT INTO worker_deliveries VALUES(?,?,?,?,?,?,?,?,?)',
            ('client-1',self.id,self.gen,'thread',None,json.dumps([mid]),'uncertain','drop',time.time()))
        calls=[]
        def call(method,params,timeout=20):
            calls.append((method,params))
            if method=='thread/resume':return {}
            self.assertEqual(method,'thread/turns/list')
            return {'data':[{'id':'recovered','status':'completed','items':[
                    {'type':'userMessage','clientId':'client-1'}]}],'nextCursor':None}
        self.supervisor.rpc.call=call
        self.supervisor.reconcile()
        self.assertEqual(store.pending_answer_count(self.con,self.id),0)
        self.assertEqual(self.con.execute('SELECT state FROM worker_deliveries').fetchone()[0],'delivered')
        self.assertEqual(store.get_task(self.con,self.id)['last_completed_turn'],'recovered')
        self.assertNotIn('turn/start',[m for m,p in calls])
        self.assertEqual(calls[-1][1]['itemsView'],'full')

    def test_missing_history_evidence_keeps_uncertain_pending(self):
        import json
        import time
        mid=self.queue();self.idle()
        self.con.execute('INSERT INTO worker_deliveries VALUES(?,?,?,?,?,?,?,?,?)',
            ('client-1',self.id,self.gen,'thread',None,json.dumps([mid]),'uncertain','drop',time.time()))
        self.supervisor.history=lambda _: []
        self.supervisor.reconcile()
        self.assertTrue(self.supervisor.flush()['uncertain'])
        self.assertEqual(store.pending_answer_count(self.con,self.id),1)
        self.assertNotIn('turn/start',[m for m,p in self.supervisor.rpc.calls])

    def test_health_requires_both_owning_processes_and_rpc(self):
        with patch.object(app_worker,'process',return_value=None):
            self.assertEqual(app_worker.health(self.task),'dead')
        with patch.object(app_worker,'process',side_effect=[object(),None]):
            self.assertEqual(app_worker.health(self.task),'unknown')
        with patch.object(app_worker,'process',return_value=object()),patch.object(
                app_worker,'control',side_effect=ConnectionError('drop')):
            self.assertEqual(app_worker.health(self.task),'unknown')

    def test_failed_start_has_no_owned_process_and_keeps_pending_worktree(self):
        import os
        fake=self.root/'fake-codex';fake.write_text('#!/bin/sh\nexit 2\n');fake.chmod(0o700)
        store.update_task(self.con,self.id,codex_bin=str(fake),worktree=str(self.root))
        self.queue()
        with patch.dict(os.environ,{'ORCHD_HOME':str(self.root/'state')}):
            with self.assertRaises(RuntimeError):
                app_worker.start(self.con,self.rt,store.get_task(self.con,self.id),'test','gpt-6.1-sol')
        task=store.get_task(self.con,self.id)
        self.assertIsNone(app_worker.process(task['supervisor_identity']))
        self.assertIsNone(app_worker.process(task['server_identity']))
        self.assertEqual(store.pending_answer_count(self.con,self.id),1)
        self.assertTrue(self.root.exists())

    def test_superseded_supervisor_cannot_mutate_new_attempt(self):
        store.update_task(self.con,self.id,generation="b"*32,active_turn="new-turn")
        with self.assertRaises(RuntimeError):self.supervisor.update(active_turn=None,turn_state="completed")
        self.complete()
        self.assertEqual(store.get_task(self.con,self.id)["active_turn"],"new-turn")

    def test_mcp_dispatch_routes_only_explicit_opt_in(self):
        args=dict(repo="demo",title="T",instructions="i",done_when="d",model_reason="r",task_type="code")
        with patch.object(app_worker,"start") as start:
            mcp_server.call("dispatch",dict(args,backend="app-server"),"thread-A",self.con,self.rt)
        start.assert_called_once()
        self.assertEqual(start.call_args.args[2]["backend"],"app-server")
        self.assertIn("end your turn right away",start.call_args.kwargs["brief"])
