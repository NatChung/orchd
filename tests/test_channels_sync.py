import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from orchd.channels import policy, service
from orchd.channels.sync import fixed_window

class SyncTest(unittest.TestCase):
    def test_window_does_not_precede_activation(self):
        activation = datetime(2026,1,1,tzinfo=timezone.utc)
        now = datetime(2026,1,2,tzinfo=timezone.utc)
        self.assertEqual(fixed_window(None, activation, now, 60), (activation, now))

    def test_source_lease_fences_poll_and_sync(self):
        with tempfile.TemporaryDirectory() as d:
            now = datetime(2026,1,2,tzinfo=timezone.utc)
            a = service.ChannelService(Path(d)/'db', clock=lambda:now, channel_home=Path(d)/'channel')
            b = service.ChannelService(Path(d)/'db', clock=lambda:now, channel_home=Path(d)/'channel')
            try:
                a.provision_source({'id':'s','host_id':'h','provider':'gmail','account_ref':'a','adapter_id':'adapter','enabled':True}, caller=policy.Caller(role='operator'))
                adapter=policy.Caller(role='adapter', sources=frozenset({'s'}), identity='adapter')
                first=a.acquire_source_lease('s', 'poll', caller=adapter, seconds=60)
                self.assertIsNone(b.acquire_source_lease('s', 'request', caller=adapter, seconds=60))
                a.release_source_lease('s', 'poll', first, caller=adapter)
                second=b.acquire_source_lease('s', 'request', caller=adapter, seconds=60)
                self.assertGreater(second, first)
                with self.assertRaises(PermissionError):
                    a.release_source_lease('s', 'poll', first, caller=adapter)
            finally:
                a.close(); b.close()
    def test_poll_and_requested_sync_share_one_lease(self):
        import threading
        from orchd.channels.adapters.email import EmailAdapter
        from orchd.channels.secret_store import FakeSecretStore
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'db'; home=Path(d)/'channel'
            now=datetime(2026,1,2,tzinfo=timezone.utc)
            svc=service.ChannelService(path,clock=lambda:now,channel_home=home)
            entered=threading.Event(); release=threading.Event(); errors=[]
            class BlockingTransport:
                def list_messages(self,start,end,page_token=None):
                    entered.set()
                    if not release.wait(5): raise RuntimeError('test timed out')
                    return {}
            try:
                svc.provision_source({'id':'s','host_id':'h','provider':'gmail','account_ref':'a','adapter_id':'adapter','enabled':True,'credential_refs':{'read':'os-store:email/read'}},caller=policy.Caller(role='operator'))
                caller=policy.Caller(role='adapter',sources=frozenset({'s'}),identity='adapter')
                worker=policy.Caller(role='worker',sources=frozenset({'s'}),identity='worker')
                secrets=FakeSecretStore({('adapter','s','os-store:email/read'):b'fake'})
                def poll():
                    own=service.ChannelService(path,clock=lambda:now,channel_home=home)
                    try:
                        EmailAdapter(own,'s',caller,BlockingTransport(),secrets,activation=now,clock=lambda:now).poll()
                    except Exception as error: errors.append(error)
                    finally: own.close()
                thread=threading.Thread(target=poll); thread.start()
                self.assertTrue(entered.wait(5))
                job=svc.request_sync('s','request',{},caller=worker)
                other=EmailAdapter(svc,'s',caller,BlockingTransport(),secrets,activation=now,clock=lambda:now)
                self.assertEqual(other.run_pending()['status'],'busy')
                self.assertEqual(svc.sync_status(job['job_id'],caller=worker)['state'],'pending')
                release.set(); thread.join(5)
                self.assertFalse(thread.is_alive()); self.assertFalse(errors)
                self.assertEqual(other.run_pending()['status'],'succeeded')
                self.assertEqual(svc.sync_status(job['job_id'],caller=worker)['state'],'succeeded')
            finally:
                release.set(); svc.close()
    def test_runner_honors_retry_after(self):
        from orchd.channels.adapters.runner import AdapterRunner
        class RateLimit(Exception): retry_after=45
        class Failing:
            def run_pending(self): raise RateLimit()
        class Stop:
            def __init__(self): self.delays=[]
            def is_set(self): return bool(self.delays)
            def wait(self,delay): self.delays.append(delay)
        stop=Stop(); AdapterRunner(Failing(),interval=1,max_backoff=10,jitter=lambda:0.5).run(stop)
        self.assertGreaterEqual(stop.delays[0],45)
    def test_long_poll_renews_lease_between_platform_calls(self):
        from datetime import timedelta
        from orchd.channels.adapters.email import EmailAdapter
        from orchd.channels.secret_store import FakeSecretStore
        with tempfile.TemporaryDirectory() as d:
            current=[datetime(2026,1,2,tzinfo=timezone.utc)]
            svc=service.ChannelService(Path(d)/'db',clock=lambda:current[0],channel_home=Path(d)/'channel')
            class Slow:
                def list_messages(self,start,end,page_token=None):
                    current[0]+=timedelta(minutes=10)
                    return {'messages':[{'id':'m'}]}
                def get_message(self,id):
                    current[0]+=timedelta(minutes=10)
                    return {'id':id,'threadId':'t','internalDate':'1767225600000','snippet':'x'}
            try:
                svc.provision_source({'id':'s','host_id':'h','provider':'gmail','account_ref':'a','adapter_id':'adapter','enabled':True,'credential_refs':{'read':'os-store:email/read'}},caller=policy.Caller(role='operator'))
                caller=policy.Caller(role='adapter',sources=frozenset({'s'}),identity='adapter')
                store=FakeSecretStore({('adapter','s','os-store:email/read'):b'fake'})
                result=EmailAdapter(svc,'s',caller,Slow(),store,activation=datetime(2026,1,1,tzinfo=timezone.utc),clock=lambda:current[0]).poll()
                self.assertEqual(result['status'],'succeeded')
            finally: svc.close()


class LostLeaseFailureTest(unittest.TestCase):
    def test_original_failure_survives_expired_lease(self):
        from orchd.channels.adapters.email import EmailAdapter
        from orchd.channels.secret_store import FakeSecretStore
        with tempfile.TemporaryDirectory() as d:
            now=[datetime(2026,1,2,tzinfo=timezone.utc)]
            svc=service.ChannelService(Path(d)/'db',clock=lambda:now[0],channel_home=Path(d)/'channel')
            class Failing:
                def list_messages(self,*args,**kwargs):
                    now[0]+=timedelta(minutes=16)
                    raise RuntimeError('original transport failure')
            try:
                svc.provision_source({'id':'s','host_id':'h','provider':'gmail','account_ref':'a','adapter_id':'adapter','enabled':True,'credential_refs':{'read':'os-store:email/read'}},caller=policy.Caller(role='operator'))
                adapter=policy.Caller(role='adapter',sources=frozenset({'s'}),identity='adapter')
                worker=policy.Caller(role='worker',sources=frozenset({'s'}),identity='worker')
                job=svc.request_sync('s','request',{},caller=worker)
                secrets=FakeSecretStore({('adapter','s','os-store:email/read'):b'fake'})
                with self.assertRaisesRegex(RuntimeError,'original transport failure'):
                    EmailAdapter(svc,'s',adapter,Failing(),secrets,activation=now[0],clock=lambda:now[0]).run_pending()
                result=svc.sync_status(job['job_id'],caller=worker)
                self.assertEqual((result['state'],result['error_code']),('failed','sync_failed'))
            finally: svc.close()
