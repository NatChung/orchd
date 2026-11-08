import re
import unittest

from orchd import core


class ElevationGuidanceTest(unittest.TestCase):
    def test_worker_briefs_require_verified_self_serve_before_elevation(self):
        for kind in ("claude", "codex"):
            with self.subTest(kind=kind):
                brief = re.sub(r"\s+", " ", core.worker_brief("orchd", kind))
                for phrase in ("within the task's authorization", "user-scope installs", "portable archives",
                               "user PATH", "wsl -u root", "containers in WSL", "verify current capabilities",
                               "Start-Process -Verb RunAs", "tried and why they failed", "sudo"):
                    self.assertIn(phrase, brief)
