import os
import socket
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from orchd import unix_socket
from orchd.platform_process import make_private, validate_private


class WorkerSocketTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows junctions")
    def test_private_directory_rejects_junction(self):
        with tempfile.TemporaryDirectory(prefix="oa-") as tmp:
            root = Path(tmp)
            target = root / "target"
            target.mkdir()
            make_private(target)
            validate_private(target)
            junction = root / "junction"
            subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(target)],
                           check=True, capture_output=True)
            with self.assertRaises(ValueError):
                validate_private(junction)

    def test_private_directory_and_socket_exchange(self):
        with tempfile.TemporaryDirectory(prefix="oa-") as tmp:
            directory = Path(tmp)
            make_private(directory)
            validate_private(directory)
            path = str(directory / "control.sock")
            errors = []
            with unix_socket.create() as listener:
                unix_socket.bind(listener, path)
                listener.listen(1)
                listener.settimeout(2)

                def send():
                    try:
                        with unix_socket.create() as peer:
                            unix_socket.connect(peer, path, timeout=2)
                            peer.sendall(b"probe")
                    except Exception as exc:
                        errors.append(exc)

                client = threading.Thread(target=send)
                client.start()
                try:
                    peer, _ = unix_socket.accept(listener)
                    with peer:
                        peer.settimeout(2)
                        self.assertEqual(peer.recv(5), b"probe")
                finally:
                    client.join(timeout=3)
                self.assertFalse(client.is_alive())
                self.assertEqual(errors, [])
