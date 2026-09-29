import importlib.util
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location('process_guard', Path(__file__).resolve().parents[1] / 'bin/process_guard.py')
g = importlib.util.module_from_spec(spec)
spec.loader.exec_module(g)


class ProcessGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.created = {}
        self.roots = []
        self.addCleanup(self.cleanup_processes)

    def cleanup_processes(self):
        # Test-owned identities only, including intentionally escaped test children.
        rows = g.inventory()
        for pid, start in self.created.items():
            if pid in rows and rows[pid]['start'] == start:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        for child in self.roots:
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=2)

    def launch(self, script='import time; time.sleep(60)'):
        child = subprocess.Popen([sys.executable, '-c', script], start_new_session=True,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.roots.append(child)
        row = self.wait_for(lambda: g.inventory().get(child.pid))
        self.created[child.pid] = row['start']
        return child, row

    def wait_for(self, callback):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = callback()
            if result:
                return result
            time.sleep(.02)
        self.fail('test subprocess did not become ready')

    def remember(self, path):
        pid = int(self.wait_for(lambda: path.read_text() if path.exists() and path.read_text() else None))
        row = self.wait_for(lambda: g.inventory().get(pid))
        self.created[pid] = row['start']
        return row

    def test_owned_group_stops_root_child_and_grandchild(self):
        child_file, grand_file = self.path / 'child', self.path / 'grand'
        grand = "import time; time.sleep(60)"
        middle = "import subprocess,sys,time,pathlib; p=subprocess.Popen([sys.executable,'-c',%r]); pathlib.Path(%r).write_text(str(p.pid)); time.sleep(60)" % (grand, str(grand_file))
        root_script = "import subprocess,sys,time,pathlib; p=subprocess.Popen([sys.executable,'-c',%r]); pathlib.Path(%r).write_text(str(p.pid)); time.sleep(60)" % (middle, str(child_file))
        root, row = self.launch(root_script)
        child = self.remember(child_file)
        grandchild = self.remember(grand_file)
        snapshot = g.capture(root.pid, row['start'], owned_group=True)
        self.assertEqual({r['pid'] for r in snapshot['observed']}, {root.pid, child['pid'], grandchild['pid']})
        result = g.stop(snapshot, term_timeout=.2, kill_timeout=.2)
        self.assertTrue(result['scope_stopped'], result)
        self.assertFalse(result['comprehensive_proof'])
        self.assertTrue(result['requires_confirmation'])

    def test_orphan_same_session_discovered_after_parent_exits(self):
        child_file = self.path / 'child'
        script = "import subprocess,sys,time,pathlib; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); pathlib.Path(%r).write_text(str(p.pid)); time.sleep(60)" % str(child_file)
        root, row = self.launch(script)
        orphan = self.remember(child_file)
        snapshot = g.capture(root.pid, row['start'], owned_group=True)
        root.terminate()
        root.wait(timeout=2)
        self.assertNotEqual(g.inventory()[orphan['pid']]['ppid'], root.pid)
        result = g.stop(snapshot, term_timeout=.1, kill_timeout=.2)
        self.assertTrue(result['scope_stopped'], result)

    def test_observed_setsid_escape_is_stopped_but_never_proven_complete(self):
        child_file = self.path / 'child'
        script = "import subprocess,sys,time,pathlib; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],start_new_session=True); pathlib.Path(%r).write_text(str(p.pid)); time.sleep(60)" % str(child_file)
        root, row = self.launch(script)
        escaped = self.remember(child_file)
        snapshot = g.capture(root.pid, row['start'], owned_group=True)
        root.terminate()
        root.wait(timeout=2)
        result = g.stop(snapshot, term_timeout=.1, kill_timeout=.2)
        self.assertTrue(result['scope_stopped'], result)
        self.assertIn(escaped['pid'], [r['pid'] for r in result['escaped']])
        self.assertFalse(result['comprehensive_proof'])

    def test_unobserved_detached_escape_requires_confirmation_even_if_scope_empty(self):
        gate, child_file = self.path / 'gate', self.path / 'child'
        script = ("import subprocess,sys,time,pathlib\n"
                  "while not pathlib.Path(%r).exists(): time.sleep(.01)\n"
                  "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],start_new_session=True)\n"
                  "pathlib.Path(%r).write_text(str(p.pid))\n") % (str(gate), str(child_file))
        root, row = self.launch(script)
        snapshot = g.capture(root.pid, row['start'], owned_group=True)
        gate.touch()
        escaped = self.remember(child_file)
        root.wait(timeout=2)
        result = g.stop(snapshot, term_timeout=0, kill_timeout=0)
        self.assertTrue(result['scope_stopped'])
        self.assertFalse(result['comprehensive_proof'])
        self.assertTrue(result['requires_confirmation'])
        self.assertTrue(g.live(g.inventory().get(escaped['pid'])))
        self.assertNotIn(escaped['pid'], [r['pid'] for r in result['signals']])

    def test_identity_mismatch_does_not_signal_reused_pid(self):
        root, row = self.launch()
        snapshot = g.capture(root.pid, row['start'], owned_group=True)
        snapshot['root']['start'] = 'different-start'
        snapshot['observed'][0]['start'] = 'different-start'
        with mock.patch.object(g.os, 'kill') as kill:
            result = g.stop(snapshot, term_timeout=0, kill_timeout=0)
        kill.assert_not_called()
        self.assertFalse(result['scope_stopped'])
        self.assertTrue(result['identity_mismatches'])
        self.assertIsNone(root.poll())

    def test_kill_failure_leaves_lingering_writer_and_blocks_evidence(self):
        root, row = self.launch('import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)')
        time.sleep(.1)
        snapshot = g.capture(root.pid, row['start'], owned_group=True)
        with mock.patch.object(g.os, 'kill', side_effect=PermissionError('test denied')):
            result = g.stop(snapshot, term_timeout=0, kill_timeout=0)
        self.assertFalse(result['scope_stopped'])
        self.assertEqual([r['pid'] for r in result['remaining']], [root.pid])
        self.assertTrue(result['errors'])

    def test_term_ignoring_writer_requires_kill(self):
        ready = self.path / 'ready'
        root, row = self.launch("import signal,time,pathlib; signal.signal(signal.SIGTERM,signal.SIG_IGN); pathlib.Path(%r).touch(); time.sleep(60)" % str(ready))
        self.wait_for(ready.exists)
        result = g.stop(g.capture(root.pid, row['start'], owned_group=True), term_timeout=.1, kill_timeout=.3)
        self.assertTrue(result['scope_stopped'], result)
        self.assertIn(int(signal.SIGKILL), [r['signal'] for r in result['signals']])

    def test_manager_identity_is_protected(self):
        own = g.inventory()[os.getpid()]
        with self.assertRaisesRegex(RuntimeError, 'protected'):
            g.capture(own['pid'], own['start'])


if __name__ == '__main__':
    unittest.main()
