import subprocess
import unittest

from orchd.upgrade import selected_remote_commit


class SelectedRemoteCommitTests(unittest.TestCase):
    def resolve(self, ref, output):
        return selected_remote_commit("https://example.invalid/orchd.git", "rev=" + ref,
                                      lambda cmd: subprocess.CompletedProcess(cmd, 0, output, ""))

    def test_branch_requires_exact_ref_despite_ls_remote_tail_matching(self):
        self.assertEqual(self.resolve("foo", "a\trefs/heads/foo\nb\trefs/heads/feature/foo\n"), "a")
        self.assertIsNone(self.resolve("foo", "b\trefs/heads/feature/foo\n"))

    def test_annotated_tag_uses_peeled_commit(self):
        self.assertEqual(self.resolve("release", "tag\trefs/tags/release\ncommit\trefs/tags/release^{}\n"), "commit")

    def test_conflicting_exact_branch_and_tag_require_uv_resolution(self):
        self.assertIsNone(self.resolve("release", "a\trefs/heads/release\nb\trefs/tags/release\n"))

    def test_fully_qualified_ref_matches_only_its_exact_name(self):
        self.assertEqual(self.resolve("refs/heads/foo", "a\trefs/heads/foo\nb\trefs/tags/foo\n"), "a")

    def test_immutable_revision_needs_no_remote_lookup(self):
        def unexpected(cmd):
            self.fail("an immutable revision should not query the remote")
        revision = "a" * 40
        self.assertEqual(selected_remote_commit("unused", "rev=" + revision, unexpected), revision)
