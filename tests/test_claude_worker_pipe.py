"""Claude worker messaging endpoints: a named pipe on Windows, a socket file elsewhere."""
import unittest
from unittest.mock import patch

from orchd import runtime
from orchd.runtime import Runtime

PIPE = r"^\\\\\.\\pipe\\orchd-[a-zA-Z0-9-]+$"  # the only shape windows_pipe.open_pipe accepts


class ClaudeWorkerPipeTest(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(Runtime, "socket_path", side_effect=lambda task_id: f"/tmp/orchd-{task_id}/w.sock")
        self.socket_path = patcher.start()
        self.addCleanup(patcher.stop)
        self.rt = Runtime.__new__(Runtime)  # path naming needs no binaries

    def test_windows_gets_a_fresh_named_pipe(self):
        # Claude on Windows exits before init when --messaging-socket-path is a file path.
        with patch.object(runtime.os, "name", "nt"):
            first = self.rt.worker_socket("ab12cd34")
            again = self.rt.worker_socket("ab12cd34")
            retry = self.rt.worker_socket("ab12cd34", "r2-0f0f0f")
        for pipe in (first, again, retry):
            self.assertRegex(pipe, PIPE)
        self.assertTrue(first.startswith("\\\\.\\pipe\\orchd-w-ab12cd34-"))
        self.assertTrue(retry.startswith("\\\\.\\pipe\\orchd-w-ab12cd34-r2-0f0f0f-"))
        self.assertNotEqual(first, again)  # never reuse a stale pipe name
        self.assertEqual(self.socket_path.call_count, 3)  # the task's private log directory is still made

    def test_posix_keeps_the_socket_file_in_the_task_directory(self):
        with patch.object(runtime.os, "name", "posix"):
            self.assertEqual(self.rt.worker_socket("ab12cd34"), "/tmp/orchd-ab12cd34/w.sock")
            self.assertEqual(self.rt.worker_socket("ab12cd34", "r2-0f0f0f").replace("\\", "/"),
                             "/tmp/orchd-ab12cd34/w-r2-0f0f0f.sock")


if __name__ == "__main__":
    unittest.main()
