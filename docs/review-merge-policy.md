# Review and merge policy for workers (issue #8)

Every worker in a repo pushes as the same GitHub account, and GitHub refuses `gh pr review --approve` /
`--request-changes` on your own account's PR. So review tasks do this instead (text lives in `worker_brief`,
`orchd/core.py`; tests in `tests/test_review_policy.py`):

1. A review task is a different task from the PR's author. The author never merges.
2. The reviewer checks the PR's current head SHA, latest main, the task's `done_when`, and the tests.
3. The verdict is recorded with `gh pr review <pr> --comment --body "<PASS or problems; reviewed SHA <sha>>"`.
   That comment is the review record. Never `--approve`, `--request-changes`, `--admin`, or any protection bypass.
4. Merge happens only if the review task's own instructions say to merge. Default stays: no merge.
   If they do and the verdict is PASS: `gh pr merge <pr> --match-head-commit <reviewed SHA>`.
5. If GitHub still requires an approval or check that is not met, the reviewer reports `blocked`.

Nat's 2026-10 decision to let this batch's independent review passes auto-merge is carried in those tasks'
instructions, not in the generic brief.

## Not changed here (remaining gap for #8)

The Orch's shared `AGENTS.md` / config (outside this repo) should get the same wording: dispatch review tasks as
separate tasks, say explicitly "review and merge" only when merge is authorized, and ask for comment + SHA as
evidence. That needs a follow-up edit by whoever owns those files, so #8 stays open (partial).
