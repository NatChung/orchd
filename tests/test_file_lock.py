import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from orchd.file_lock import try_lock


class FileLockTests(unittest.TestCase):
    def test_contention_and_release_after_process_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "delivery.lock"
            script = """
import sys
from orchd.file_lock import try_lock
handle = open(sys.argv[1], "a")
try_lock(handle)
print("locked", flush=True)
sys.stdin.read()
"""
            with subprocess.Popen([sys.executable, "-c", script, str(path)],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True) as child:
                try:
                    self.assertEqual(child.stdout.readline().strip(), "locked")
                    with path.open("a") as contender:
                        with self.assertRaises(BlockingIOError):
                            try_lock(contender)
                        child.kill()
                        child.wait(timeout=10)
                        try_lock(contender)
                finally:
                    child.kill()
