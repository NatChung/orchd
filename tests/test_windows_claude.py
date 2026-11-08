import os
import unittest
from unittest.mock import patch

from orchd.runtime import Runtime


@unittest.skipUnless(os.name == "nt", "Windows Claude transport")
class WindowsClaudeTests(unittest.TestCase):
    def test_unverified_message_is_not_reported_delivered(self):
        with patch("orchd.windows_pipe.send"), patch("orchd.windows_pipe.wait_for_receipt", return_value=False):
            with self.assertRaisesRegex(OSError, "receipt was not confirmed"):
                Runtime().send_uds(r"\\.\pipe\orchd-test", "session", "test")

    def test_message_requires_matching_transcript_uuid(self):
        from orchd.windows_pipe import wait_for_receipt
        import json
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "projects" / "example"
            project.mkdir(parents=True)
            (project / "session.jsonl").write_text(json.dumps({
                "type": "user", "uuid": "real-id", "message": {"content": "test"}
            }) + "\n", encoding="utf-8")
            with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": tmp}):
                self.assertTrue(wait_for_receipt("session", "real-id", timeout=.1))
                self.assertFalse(wait_for_receipt("session", "wrong-id", timeout=.1))

    def test_mid_turn_queued_command_counts_as_receipt(self):
        from orchd.windows_pipe import wait_for_receipt
        import json
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "projects" / "example"
            project.mkdir(parents=True)
            (project / "session.jsonl").write_text("\n".join(json.dumps(x) for x in [
                {"type": "queue-operation", "operation": "enqueue", "sessionId": "session", "content": "test"},
                {"type": "attachment", "uuid": "new-id", "attachment": {
                    "type": "queued_command", "prompt": "test", "source_uuid": "frame-id", "delivery_id": "d"}},
            ]) + "\n", encoding="utf-8")
            with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": tmp}):
                self.assertTrue(wait_for_receipt("session", "frame-id", timeout=.1))
                self.assertFalse(wait_for_receipt("session", "new-id", timeout=.1))
                self.assertFalse(wait_for_receipt("session", "other-id", timeout=.1))

    def test_trust_lookup_normalizes_windows_slashes(self):
        import json
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / ".claude.json"
            config.write_text(json.dumps({"projects": {
                "C:/Users/Test/orch/home": {"hasTrustDialogAccepted": True}
            }}), encoding="utf-8")
            with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": tmp}):
                self.assertTrue(Runtime().claude_trusted(r"c:\users\test\orch\home"))
                self.assertFalse(Runtime().claude_trusted(r"c:\users\test\orch\other"))
