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

Operator's 2026-10 decision to let this batch's independent review passes auto-merge is carried in those tasks'
instructions, not in the generic brief.

## Coverage and what is not verified

Covered in this repo (source: `worker_brief` in `orchd/core.py`, tests in `tests/test_review_policy.py`):
review task is separate from the author's task, verdict via `--comment` with the reviewed SHA, merge only when the
review task says so, using `--match-head-commit`, `blocked` when GitHub still requires something.

Covered by the instructions passed in for this batch (task text, not a file in this repo): independent review
worker checks against `done_when` and merges only on PASS.

Not verified: whether the Orch's shared `AGENTS.md` / config (outside this repo) already says the same or lacks
it. Nobody has read it for this change, so no gap is claimed and no edit to it is requested here. If someone
checks it and finds a concrete missing rule, open a separate issue with that evidence.
