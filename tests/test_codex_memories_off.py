"""Codex memory consolidation must stay off for workers orchd starts (issue #50)."""
import tempfile
import unittest
from pathlib import Path

from orchd import app_worker
from orchd.runtime import Runtime

OFF = ['-c', 'features.memories=false']


def has_override(cmd):
    return any(cmd[i:i + 2] == OFF for i in range(len(cmd)))


class CodexMemoriesOffTest(unittest.TestCase):
    def test_app_server_command_disables_memories(self):
        cmd = app_worker.app_server_command({'codex_bin': '/fake/codex', 'endpoint': '/s/app.sock'})
        self.assertEqual(cmd[0], '/fake/codex')
        self.assertTrue(has_override(cmd), cmd)
        self.assertLess(cmd.index('-c'), cmd.index('app-server'))
        self.assertEqual(cmd[-2:], ['--listen', 'unix:///s/app.sock'])

    def test_exec_start_and_resume_disable_memories(self):
        rt = Runtime()
        rt.codex = '/fake/codex'
        seen = []

        def spawn(cmd, cwd, path):
            seen.append(cmd)
            Path(path).write_text('{"type":"thread.started","thread_id":"th-1"}\n')
            return 99
        rt.spawn, rt.pid_alive, rt.sleep = spawn, lambda pid: True, lambda s: None
        with tempfile.TemporaryDirectory() as tmp:
            rt.start_codex_worker('/wt', str(Path(tmp) / 'codex.jsonl'), 'prompt', 'gpt-6.1-sol')
            rt.resume_codex_worker('/wt', str(Path(tmp) / 'codex.jsonl'), 'th-1', 'answer', 'gpt-6.1-sol')
        self.assertEqual(len(seen), 2)
        for cmd in seen:
            self.assertTrue(has_override(cmd), cmd)


if __name__ == '__main__':
    unittest.main()
