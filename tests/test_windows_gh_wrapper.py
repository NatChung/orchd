import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchd.runtime import worker_env


@unittest.skipUnless(os.name == "nt", "Windows command launcher")
class WindowsGhWrapperTests(unittest.TestCase):
    def test_powershell_uses_wrapper_for_pr(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            real_bin = directory / "real"
            real_bin.mkdir()
            (real_bin / "gh.cmd").write_text("@echo off\r\necho real-gh %*\r\n")
            with patch.dict(os.environ, {"ORCHD_HOME": str(directory / "home"),
                                        "ORCHD_WORKTREE": str(directory),
                                        "PATH": str(real_bin) + os.pathsep + os.environ["PATH"]}):
                env = worker_env(directory)
                env["ORCHD_BIN"] = str(directory / "missing.exe")
                result = subprocess.run(["powershell.exe", "-NoProfile", "-Command",
                                         "gh --version; exit $LASTEXITCODE"],
                                        capture_output=True, text=True, env=env)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("real-gh --version", result.stdout)
                refused = subprocess.run(["powershell.exe", "-NoProfile", "-Command",
                                          "gh pr create; exit $LASTEXITCODE"],
                                         capture_output=True, text=True, env=env)
                self.assertEqual(refused.returncode, 0)
                self.assertIn("real-gh pr create", refused.stdout)
