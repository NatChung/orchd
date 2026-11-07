"""Worker Git helper configuration for both host platforms."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchd import runtime


class WorkerGitHelperTest(unittest.TestCase):
    def test_windows_uses_quoted_launcher_and_preserves_routing(self):
        with tempfile.TemporaryDirectory(prefix="orchd space ") as root:
            launcher = Path(root) / "gh.cmd"
            launcher.touch()
            with patch.dict(os.environ, {"PATH": "original", "GH_REPO": "Owner/repo",
                                         "ORCHD_GH_ACCOUNTS": "accounts.json"}, clear=True), \
                    patch.object(runtime, "worker_bin_dir", return_value=root), \
                patch.object(runtime, "orchd_bin", return_value="orchd"), \
                    patch.object(runtime.os, "name", "nt"):
                env = runtime.worker_env()
            self.assertEqual(env["GIT_CONFIG_VALUE_0"], "")
            self.assertEqual(env["GIT_CONFIG_VALUE_1"], f'!"{launcher.resolve().as_posix()}" auth git-credential')
            self.assertEqual(env["GIT_CONFIG_VALUE_2"], "true")
            self.assertEqual(env["ORCHD_GH_WRAPPER_DIR"], root)
            self.assertEqual(env["GH_REPO"], "Owner/repo")
            self.assertEqual(env["ORCHD_GH_ACCOUNTS"], "accounts.json")

    def test_windows_missing_launcher_falls_back(self):
        with tempfile.TemporaryDirectory() as root, \
                patch.object(runtime, "worker_bin_dir", return_value=root), \
                patch.object(runtime, "orchd_bin", return_value="orchd"), \
                patch.object(runtime.os, "name", "nt"):
            env = runtime.worker_env()
        count = int(env["GIT_CONFIG_COUNT"])
        self.assertEqual(env[f"GIT_CONFIG_VALUE_{count - 2}"], "!gh auth git-credential")

    def test_non_windows_helper_is_unchanged(self):
        with tempfile.TemporaryDirectory() as root, \
                patch.dict(os.environ, {"PATH": "original"}, clear=True), \
                patch.object(runtime, "worker_bin_dir", return_value=root), \
                patch.object(runtime, "orchd_bin", return_value="orchd"), \
                patch.object(runtime.os, "name", "posix"):
            env = runtime.worker_env()
        self.assertEqual(env["GIT_CONFIG_VALUE_0"], "")
        self.assertEqual(env["GIT_CONFIG_VALUE_1"], "!gh auth git-credential")
        self.assertEqual(env["GIT_CONFIG_VALUE_2"], "true")
