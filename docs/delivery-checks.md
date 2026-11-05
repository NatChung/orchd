# Delivery checks (preflight and pre-PR)

Implements phase 1 of [the staged pipeline design](design/staged-pipeline.md) (#119). A product repo listed in
`[delivery_flows]` may have a Hub definition at `docs/development/delivery.pipeline.json`. Orchd fetches the Hub,
pins `origin/HEAD` and reads only `checks.preflight` and `checks.pr` (each with exactly `argv`, `cwd`,
`timeout_seconds`, `allowed_exit_codes`, `env_allowlist`, `required`). Stages are #121 and are ignored here.
A Hub without the file, or a repo not in `[delivery_flows]`, behaves as before.

- `orchd check preflight TASK` / `orchd check pr TASK` run the pinned argv (no shell) in the task worktree with only the
  allowlisted environment plus `ORCHD_TASK_ID`, `ORCHD_BASE_SHA`, `ORCHD_HEAD_SHA`, `ORCHD_CHECK_KIND` and a per-task
  test namespace: `ORCHD_TEST_NAMESPACE`, `ORCHD_TEST_DB_NAME`, `ORCHD_TEST_REDIS_PREFIX`. Each run is stored in
  `check_runs` with the Hub commit, definition SHA-256, SHAs, exit status and bounded, redacted output.
- `orchd check show TASK` and the read-only MCP tool `check_runs` list the runs.
- The worker `gh` wrapper calls `orchd check gate` before `gh pr create`. It refuses unless the latest preflight passed
  against the current definition and base, the worktree is clean, and `checks.pr` passes on the exact head. The PR
  inputs (title, body, base, head, labels, repo) are written to a JSON file named by `ORCHD_PR_INPUT`.
- A configured Hub that cannot be fetched or whose definition is invalid blocks the PR (fail closed).

The wrapper is not a security boundary: `gh api`, plain `git push` and a direct `gh` binary bypass it. Credential
isolation and the red/green, baseline and document-SHA manifests belong to #121.
