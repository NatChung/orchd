import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchd.runtime import Runtime, launch_env


class BindingFailureTest(unittest.TestCase):
    def test_cli_prints_runtime_and_os_errors_without_traceback(self):
        import contextlib
        import io
        from orchd import cli, entry
        with tempfile.TemporaryDirectory() as tmp:
            for error in (RuntimeError("socket not published"), OSError("binary missing")):
                stderr = io.StringIO()
                with mock.patch.dict(os.environ, {"ORCHD_HOME": tmp}), \
                        mock.patch.object(entry, "binding", side_effect=error), \
                        contextlib.redirect_stderr(stderr):
                    self.assertEqual(cli.main(["binding"]), 1)
                self.assertIn(str(error), stderr.getvalue())
                self.assertNotIn("Traceback", stderr.getvalue())

    def test_socket_timeout_stops_only_created_job_and_names_cause(self):
        rt = Runtime()
        def run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 0, "2.1.291" if "--version" in cmd else "claude attach job", "")
        with mock.patch.object(rt, "run", side_effect=run), \
                mock.patch.object(rt, "agents", return_value=[{"id": "job", "sessionId": "session"}]), \
                mock.patch.object(rt, "exists", return_value=False), \
                mock.patch.object(rt, "sleep"), \
                mock.patch.object(rt, "stop_worker") as stop, \
                mock.patch("orchd.runtime.time.monotonic", side_effect=[0, 0, 26]):
            with self.assertRaisesRegex(RuntimeError, "version=2.1.291.*Restricted mode"):
                rt.start_claude("/tmp", "/tmp/missing", "opus", [])
        stop.assert_called_once_with("job")

    def test_missing_binary_is_an_oserror_without_spawning(self):
        rt = Runtime()
        rt.claude = "/missing/claude"
        with self.assertRaises(OSError):
            rt.start_claude("/tmp", "/tmp/missing", "opus", [])

    def test_config_namespace_is_preserved_but_session_variables_are_removed(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "/tmp/config", "CLAUDECODE": "1"}):
            self.assertEqual(launch_env()["CLAUDE_CONFIG_DIR"], "/tmp/config")
            self.assertNotIn("CLAUDECODE", launch_env())

    def test_trust_uses_selected_config_namespace(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, ".claude.json").write_text(json.dumps({"projects": {
                "/repo": {"hasTrustDialogAccepted": True}}}))
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": tmp}):
                self.assertTrue(Runtime().claude_trusted("/repo"))
