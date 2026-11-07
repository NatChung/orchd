import os
import subprocess
import sys
import time
import unittest
import uuid

import psutil

from orchd import app_worker, windows_job


@unittest.skipUnless(os.name == "nt", "Windows Job Objects")
class WindowsWorkerJobTests(unittest.TestCase):
    def test_terminating_job_stops_supervisor_and_descendant(self):
        generation = uuid.uuid4().hex
        script = """
import os, subprocess, sys, time
from orchd import windows_job
job = windows_job.join_new(sys.argv[1])
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
print(os.getpid(), child.pid, flush=True)
time.sleep(60)
"""
        with subprocess.Popen([sys.executable, "-c", script, generation], stdout=subprocess.PIPE, text=True) as supervisor, \
             subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE) as sibling:
            try:
                owner_pid, child_pid = map(int, supervisor.stdout.readline().split())
                child = psutil.Process(child_pid)
                with self.assertRaisesRegex(RuntimeError, "outside the named worker job"):
                    windows_job.terminate(generation, psutil.Process(sibling.pid))
                self.assertIsNone(supervisor.poll())
                self.assertTrue(child.is_running())
                self.assertTrue(windows_job.terminate(generation, psutil.Process(owner_pid)))
                supervisor.wait(timeout=5)
                deadline = time.monotonic() + 5
                while child.is_running() and time.monotonic() < deadline:
                    time.sleep(.05)
                self.assertFalse(child.is_running())
                self.assertIsNone(sibling.poll())
            finally:
                sibling.communicate()
                if supervisor.poll() is None:
                    supervisor.kill()

    def test_close_after_supervisor_exit_checks_server_without_terminating_other_processes(self):
        with subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE) as old:
            identity = app_worker.identity(old.pid)
            old.communicate()
        task = {"supervisor_identity": identity, "server_identity": identity, "generation": uuid.uuid4().hex}
        self.assertIsNone(app_worker.stop(None, task))
        task["server_identity"] = app_worker.identity(os.getpid())
        with self.assertRaisesRegex(RuntimeError, "server survives"):
            app_worker.stop(None, task)
