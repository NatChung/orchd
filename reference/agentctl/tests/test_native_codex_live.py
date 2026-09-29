"""Opt-in real CLI regression; no model turns, isolated server and cwd."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest

from test_native import native


@unittest.skipUnless(os.environ.get("AGENTCTL_TEST_CODEX_LIVE") == "1" and shutil.which("codex"),
                     "set AGENTCTL_TEST_CODEX_LIVE=1 for real Codex transport checks")
class EmptyCodexAttach(unittest.TestCase):
    def test_empty_thread_can_resume_from_second_client_without_turn(self):
        with tempfile.TemporaryDirectory(prefix="act-attach-", dir="/tmp") as tmp:
            endpoint = str(Path(tmp) / "s")
            with open(Path(tmp) / "server.log", "w") as log:
                server = subprocess.Popen(["codex", "app-server", "--listen", "unix://" + endpoint],
                                          cwd=tmp, stdin=subprocess.DEVNULL, stdout=log, stderr=log)
                clients = []
                try:
                    deadline = time.monotonic() + 15
                    while not os.path.exists(endpoint):
                        self.assertIsNone(server.poll(), "server exited")
                        self.assertLess(time.monotonic(), deadline, "socket timeout")
                        time.sleep(.05)

                    def connect():
                        ws = native.UnixWebSocket(endpoint)
                        clients.append(ws)
                        ws.request("initialize", {"clientInfo": {"name": "agentctl-test", "version": "1"},
                                                  "capabilities": {"experimentalApi": True}})
                        ws.send({"method": "initialized", "params": {}})
                        return ws

                    observer = connect()
                    sid = native.codex_thread(observer, {"cwd": tmp}, None, "agentctl empty test")
                    # No turn/start or queue message: a freshly idle worker must attach.
                    for _ in range(2):
                        viewer = connect()
                        # Match TUI bootstrap: metadata-only resume must work FIRST.
                        # A full-history resume here would repair the bug and mask it.
                        thread = viewer.request("thread/resume", {"threadId": sid, "excludeTurns": True})["thread"]
                        self.assertEqual(thread["id"], sid)
                        self.assertEqual(thread["turns"], [])
                        self.assertEqual(os.path.realpath(thread["cwd"]), os.path.realpath(tmp))
                        viewer.close()
                        clients.remove(viewer)
                finally:
                    for ws in clients:
                        ws.close()
                    server.terminate()
                    try:
                        server.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        server.kill()
                        server.wait()
