"""Review/merge policy in the worker brief (#8): the brief must not push an unauthorized or author merge."""
import re
import unittest

from orchd import core

KINDS = ("claude", "codex")


def bullets(text):
    """Split the Rules list into one string per top-level bullet."""
    return [re.sub(r"\s+", " ", b).strip() for b in re.split(r"\n(?=- )", text)]


def review_bullet(kind):
    (b,) = [b for b in bullets(core.worker_brief("orchd", kind)) if b.startswith("- Reviewing a PR")]
    return b


def sentences(text):
    return [s.strip() for s in re.split(r"(?<=[.;:])\s+", text) if s.strip()]


class ReviewPolicyTest(unittest.TestCase):
    def test_merge_stays_opt_in_for_both_worker_kinds(self):
        for kind in KINDS:
            brief = re.sub(r"\s+", " ", core.worker_brief("orchd", kind))
            self.assertIn("Merge a PR only when the task explicitly tells you to review that PR and merge it", brief)
            self.assertIn("Never push directly to the default branch", brief)
            self.assertIn("Never review-and-merge a PR you authored in the same task", brief)

    def test_every_merge_command_is_gated_and_head_pinned(self):
        for kind in KINDS:
            brief = re.sub(r"\s+", " ", core.worker_brief("orchd", kind))
            merges = [s for s in sentences(brief) if "gh pr merge" in s]
            self.assertTrue(merges)
            for s in merges:
                self.assertIn("--match-head-commit", s)
                self.assertRegex(s, r"(?i)\bif they do\b|\bonly when the task explicitly\b")
                self.assertRegex(s, r"PASS")

    def test_review_without_merge_authorization_stops_after_comment(self):
        for kind in KINDS:
            b = review_bullet(kind)
            comment_at = b.index("gh pr review <pr> --comment")
            stop = b.index("do not also tell you to merge")
            merge_at = b.index("gh pr merge")
            self.assertLess(comment_at, stop)
            self.assertLess(stop, merge_at)  # the merge command only appears after the no-authorization stop
            self.assertIn("stops after the comment", b)

    def test_review_record_is_comment_with_verdict_and_sha(self):
        for kind in KINDS:
            b = review_bullet(kind)
            self.assertRegex(b, r"--comment --body \"<PASS or the problems found; reviewed SHA <sha>>\"")
            for needed in ("current head SHA", "latest main", "done_when", "tests"):
                self.assertIn(needed, b)

    def test_forbidden_review_and_bypass_flags_only_appear_as_prohibitions(self):
        for kind in KINDS:
            brief = re.sub(r"\s+", " ", core.worker_brief("orchd", kind))
            for flag in ("--approve", "--request-changes", "--admin"):
                for m in re.finditer(re.escape(flag), brief):
                    self.assertRegex(brief[: m.start()][-60:], r"never use", flag)

    def test_unmet_required_approval_or_check_means_blocked_not_workaround(self):
        for kind in KINDS:
            b = review_bullet(kind)
            self.assertRegex(b, r"requires an approval or a check that is not met, do not work around it: report blocked")

    def test_author_never_merges_and_reviewer_is_a_different_task(self):
        for kind in KINDS:
            b = review_bullet(kind)
            self.assertIn("The author of a PR never merges it", b)
            self.assertIn("reviewer is a different task", b)

    def test_task_message_never_injects_merge_authorization(self):
        task = {"id": "t1", "repo": "r", "worktree": "/w", "branch": "b", "title": "Review PR 5",
                "instructions": "Review PR 5 and comment only.", "done_when": "comment posted"}
        msg = core.task_message(task)
        self.assertNotIn("gh pr merge", msg)


if __name__ == "__main__":
    unittest.main()
