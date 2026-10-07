import json
import subprocess
import sys
from unittest.mock import patch

from orchd import app_worker, core, store
from tests.app_support import AppBase


class ReviewRegressionTest(AppBase):
    def test_default_dispatch_without_websocket(self):
        result = subprocess.run([sys.executable, '-c', '''
import sys, tempfile
sys.modules['websocket'] = None
from orchd import core, store
from tests.test_orchd import FakeRuntime
with tempfile.TemporaryDirectory() as tmp:
    con = store.connect(tmp + '/db')
    task = core.dispatch(con, FakeRuntime(), orch_thread='test', repo='demo',
        title='T', instructions='i', done_when='d', model_reason='r', task_type='code')
    assert task['backend'] == 'exec'
    con.close()
'''], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def missing_runtime(self):
        directory = self.root / 'gone'
        store.update_task(self.con, self.id, endpoint=str(directory/'app.sock'),
                          control_endpoint=str(directory/'control.sock'))
        return store.get_task(self.con, self.id)

    def test_missing_runtime_health_and_identity_cleanup(self):
        task = self.missing_runtime()
        self.assertEqual(app_worker.health(task), 'dead')
        with patch.object(app_worker, 'process', return_value=object()):
            self.assertEqual(app_worker.health(task), 'unknown')
        with patch.object(app_worker, 'stop_identity') as stop:
            app_worker.stop(self.con, task)
        self.assertEqual(stop.call_count, 2)

    def test_missing_runtime_close_completes(self):
        self.missing_runtime()
        self.assertTrue(core.close(self.con, self.rt, self.id)['closed'])
        self.assertEqual(store.get_task(self.con, self.id)['status'], 'closed')

    def test_missing_runtime_still_stops_owned_process(self):
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],
                                 start_new_session=True)
        try:
            raw = app_worker.identity(child.pid)
            store.update_task(self.con, self.id, supervisor_identity=raw)
            task = self.missing_runtime()
            self.assertEqual(app_worker.health(task), 'unknown')
            self.assertTrue(core.close(self.con, self.rt, self.id)['closed'])
            self.assertIsNone(app_worker.process(raw))
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)

    def test_missing_runtime_retry_can_start_exec(self):
        self.missing_runtime()
        task = core.retry(self.con, self.rt, self.id, 'sol', 'recover', backend='exec')
        self.assertEqual(task['backend'], 'exec')
        self.assertEqual(task['status'], 'running')

    def test_missing_socket_dead_identities_health(self):
        self.assertEqual(app_worker.health(self.task), 'dead')
        self.assertTrue(core.close(self.con, self.rt, self.id)['closed'])

    def test_item_events_keep_metadata_only(self):
        self.supervisor.event({'method':'item/completed', 'params':{'threadId':'thread',
            'turnId':'turn', 'item':{'id':'item', 'type':'commandExecution',
                'status':'completed', 'aggregatedOutput':'secret', 'command':'secret'}}})
        body = self.con.execute("SELECT body FROM messages WHERE kind='app_event'").fetchone()[0]
        self.assertNotIn('secret', body)
        self.assertEqual(json.loads(body)['params']['item']['id'], 'item')

    def test_uncertain_send_schedules_live_reconciliation(self):
        self.queue(); self.idle()
        self.supervisor.rpc.fail = TimeoutError('slow')
        self.assertTrue(self.supervisor.flush()['uncertain'])
        self.assertTrue(self.supervisor.reconcile_pending)
        delivery = self.con.execute('SELECT id FROM worker_deliveries').fetchone()[0]
        self.supervisor.rpc.fail = None
        self.supervisor.history = lambda _: [{'id':'recovered', 'status':'completed',
            'items':[{'clientId':delivery}]}]
        self.supervisor.reconcile_if_needed()
        self.assertFalse(self.supervisor.reconcile_pending)
        self.assertEqual(store.pending_answer_count(self.con, self.id), 0)
        self.assertEqual([m for m,p in self.supervisor.rpc.calls].count('turn/start'), 1)

    def test_interrupt_lock_contention_defers_correction(self):
        self.queue()
        self.supervisor.rpc.events.put({'method':'turn/completed', 'params':{
            'threadId':'thread', 'turn':{'id':'turn', 'status':'interrupted'}}})
        with patch.object(self.supervisor, 'flush', side_effect=TimeoutError('lock')):
            result = self.supervisor.interrupt()
        self.assertEqual(result['status'], 'queued')
        self.assertEqual(self.supervisor.deferred_flush, (self.gen, 'thread', 'turn'))
        self.assertEqual(self.supervisor.flush(guard=self.supervisor.deferred_flush)['delivered'], 1)
