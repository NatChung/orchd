# Delivery checks (preflight and pre-PR)

Implements phase 1 of [the staged pipeline design](design/staged-pipeline.md) (#119). A product repo listed in
`[delivery_flows]` may have a Hub definition at `docs/development/delivery.pipeline.json`. Orchd fetches the Hub,
pins `origin/HEAD` and reads `artifact_rules`, `checks.preflight` and `checks.pr` (each with exactly `argv`, `cwd`,
`timeout_seconds`, `allowed_exit_codes`, `env_allowlist`, `required`). Stages are #121 and are ignored here.
A Hub without the file, or a repo not in `[delivery_flows]`, behaves as before.

- `orchd check preflight TASK` runs the pinned argv (no shell) on a clean detached checkout of the task's base commit
  (temporary, removed afterwards); `orchd check pr TASK` runs it in the task worktree, which must be clean, so it checks
  exactly HEAD. Both run with only the
  allowlisted environment plus `ORCHD_TASK_ID`, `ORCHD_BASE_SHA`, `ORCHD_HEAD_SHA`, `ORCHD_CHECK_KIND` and a per-task
  test namespace: `ORCHD_TEST_NAMESPACE`, `ORCHD_TEST_DB_NAME`, `ORCHD_TEST_REDIS_PREFIX`. Each run is stored in
  `check_runs` with the Hub commit, definition SHA-256, SHAs, exit status and bounded, redacted output.
- `orchd check show TASK` and the read-only MCP tool `check_runs` list the runs.
- The worker `gh` wrapper calls `orchd check gate` before `gh pr create`. It refuses unless the latest preflight passed
  against the current definition and ran on the task's base commit, the worktree is clean and on the task branch,
  `origin/<branch>` (if known) equals HEAD, `--base` (if given) is a known origin branch, and `checks.pr` passes on that
  head. The PR inputs (title, body, base, head, labels, repo) are written to a JSON file named by `ORCHD_PR_INPUT`.
- `--base` must be the task's recorded base branch (`tasks.base_branch`, set at dispatch from origin/HEAD) and origin's
  copy must contain the base commit preflight ran on; orchd always passes `--base <that branch>`. A task without a
  recorded base branch is refused. Each argument except `--label`/`--reviewer`/`--assignee` may appear once, and
  `--body` and `--body-file` are mutually exclusive.
- Before comparing, the gate runs `git fetch origin <base branch>` and `git fetch origin <task branch>` (only those two
  refs) so a stale local `origin/*` cannot vouch for a rewritten remote; a failed base fetch (missing branch, unreachable
  origin) refuses. A task branch not on origin yet is allowed.
- A repo with no pipeline is never touched: the wrapper asks orchd first and runs the original `gh` command unchanged,
  whatever the task's state. Only a configured repo refuses a closed or unknown task.
- Only these `gh pr create` arguments are accepted: `--title`, `--body`, `--body-file` (not `-`), `--base`, `--draft`,
  `--label`, `--reviewer`, `--assignee`. Anything else is refused before gh runs, and `--head` and `--repo` cannot be
  given: orchd passes gh the checked body (a `--body-file` is sent as `--body`), `--head <task branch>` and
  `--repo <origin owner/repo>`.
- A configured Hub that cannot be fetched or whose definition is invalid blocks the PR (fail closed).

The wrapper finds the real subcommand (global flags `-R`, `--repo`, `--hostname`; `pr new` is `pr create`) and sends every
`pr create` through the gate. Global flags before the subcommand, or between `pr` and `create`, are refused for `pr create`,
and an unrecognized global flag is refused for any worker `gh` call (fail closed).

The wrapper is not a security boundary: `gh api` (including a POST to `/pulls` or the GraphQL `createPullRequest` mutation),
user-defined `gh alias`es, plain `git push` and a direct `gh` binary bypass it. Credential
isolation and the red/green, baseline and document-SHA manifests belong to #121.

## PR artifact rules

The optional top-level `artifact_rules` object maps orchd repo names to policies. Each policy accepts
`forbidden_globs`, `forbidden_extensions`, and `allowlist` arrays, and an `exceptions` object mapping descriptive
names to arrays of globs. All fields are optional; unknown keys and invalid values fail validation. Extensions
include the leading dot and match case-sensitively. Globs use Python `fnmatchcase` on the whole repo-relative path:
`*` can match `/`, and `**` has the same meaning as `*`. Allowlist and named exception globs override both deny
lists; keep them narrow and review their names and paths in the Hub definition. See the example in the
[staged pipeline design](design/staged-pipeline.md).

Before `checks.pr` runs, the PR gate compares the merge base of the fetched target branch and checked head to that
head. It checks added, modified and renamed destination paths, skips deletions, and lists every blocked path. A
repo without a policy is unchanged; a repo absent from `repos` remains unconfigured. This rule is part of the
wrapper gate and does not run in a standalone `orchd check pr` invocation.
