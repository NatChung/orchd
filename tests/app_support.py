"""Isolated app-server fixtures; no Codex or live DB access."""
import json
from pathlib import Path
import queue
import tempfile
import unittest
from unittest.mock import patch

from orchd import app_worker, store
from tests.test_orchd import FakeRuntime


class FakeRPC:
    def __init__(self):
        self.events=queue.Queue();self.calls=[];self.error=None;self.fail=None

    def call(self,method,params,timeout=20):
        self.calls.append((method,params))
        if self.fail:raise self.fail
        if method=='turn/start':return {'turn':{'id':'next-turn','status':'inProgress'}}
        if method=='thread/read':return {'thread':{'id':'thread','status':{'type':'idle'},'turns':[]}}
        return {}


class AppBase(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.root.chmod(0o700)
        self.db=self.root/'db';self.con=store.connect(self.db);self.rt=FakeRuntime();self.rt.exists=lambda _: True
        self.id='app-test';self.gen='a'*32
        self.task=store.create_task(self.con,id=self.id,repo='demo',repo_path='/tmp/demo',title='T',
            instructions='i',done_when='d',orch_thread='thread-A',codex_bin='codex',model='gpt-6.1-sol',
            worktree='/tmp/app-test-wt',status='running',backend='app-server',generation=self.gen,
            endpoint=str(self.root/'app.sock'),control_endpoint=str(self.root/'control.sock'),
            session_id='thread',active_turn='turn',turn_state='active',job_id='123')
        self.supervisor=app_worker.Supervisor(self.db,self.id,self.gen)
        self.supervisor.rpc=FakeRPC()

    def tearDown(self):
        self.supervisor.con.close();self.con.close();self.tmp.cleanup()

    def queue(self,text='queued'):
        return store.add_message(self.con,self.id,store.QUEUED,text)

    def complete(self,turn='turn',status='completed',thread='thread'):
        self.supervisor.event({'method':'turn/completed','params':{'threadId':thread,
                              'turn':{'id':turn,'status':status,'error':None}}})

    def idle(self):
        store.update_task(self.con,self.id,active_turn=None,turn_state='completed',last_completed_turn='turn')
