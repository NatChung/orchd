# Worker delivery flow adapter

The task scope and orchd's worker rules govern all work. Follow the configured delivery flow as well when available; report missing guidance instead of assuming it does not apply.

Use only the pstack how, why, tdd and blast-radius methods where applicable. Read the relevant `SKILL.md` directly; do not invoke slash commands. This host's defaults are configurable, not guaranteed: Claude uses `~/.claude/skills/pstack-X/SKILL.md` and Codex uses `~/.agents/skills/pstack-X/SKILL.md`, where X is the method name. Report an unavailable skill as a gap.

Do the work yourself: do not start subagents, conduct cross-vendor reviews, send outward messages, or merge. Where a skill asks for those actions, do the applicable investigation yourself or report the gap. Follow orchd's preview and approval rules for outward actions.

- For how, ask who downstream uses each affected field and what happens to existing data.
- For why, distinguish evidence from inference and report evidence gaps.
- For tdd, use a failing behavior test first when practical.
- For blast-radius, verify the important safety facts with real code. For every identified risk, record whether you will change it or not, and why; ask before work outside scope.
- After selecting the base, run `codegraph init -y` or `codegraph sync` before code queries, according to the index state.
- Compare test results with a baseline from the same base; report failures and differences.
- If the diff touches locale or translation files, list new keys, changed existing values, who pushes to the translation platform (POEditor for web-next), whether non-English locales changed, and the risk of existing values being overwritten by the next pull in both the report and PR body. Do not edit non-English locales unless the ticket says so.
- For copy tickets, list every sibling state of each changed string (for example, Show more/Show less or Add to bag/Go to bag) and ask the PM via `orchd ask` before deciding.
- If a ticket attachment or required source is unavailable and the PM has not explicitly allowed a best guess, report blocked with evidence; do not make speculative commits.
- For pages using SSR, ISR, or static generation, state whether server output is affected and verified; otherwise list it as unverified.
- Treat only expected not-found responses (for example, 404) as missing config; log and surface 5xx and network errors.
- Use PR title `type(TICKET-KEY): summary`, with a Conventional Commits type matching the runtime change.
- For cross-service work, the ticket must name the backend contract source and owner; otherwise mark the work provisional and do not report it complete.

## mobile-flutter-app PR gate

When its Hub pipeline lists `mobile-flutter-app`, dispatch defaults to the highest numeric `release-X.Y.Z` branch on origin. An explicit `base_branch` overrides it; if no release branch exists, dispatch fails and reports the missing branch. The PR wrapper uses the recorded task base. Unlike web-next, APP compares Flutter analysis and test failures with the recorded base in the PR check, and does not add browser or device acceptance. Existing failures are tolerated; new diagnostics or failures block the PR. The comparison checks out the base in a temporary worktree and runs both commands on base and head; unrecognized diagnostics or incomplete test output fails closed.

Hub configuration proposal (merge with the existing pipeline; do not replace other repos' settings):

```json
{
  "repo_checks": {
    "mobile-flutter-app": {
      "preflight": {
        "argv": ["flutter", "--version"],
        "cwd": ".",
        "timeout_seconds": 3600,
        "allowed_exit_codes": [0],
        "env_allowlist": ["PATH", "HOME"],
        "required": true
      },
      "pr": {
        "argv": ["orchd-flutter-gate"],
        "cwd": ".",
        "timeout_seconds": 3600,
        "allowed_exit_codes": [0],
        "env_allowlist": ["PATH", "HOME"],
        "required": true
      }
    }
  },
  "artifact_rules": {
    "mobile-flutter-app": {
      "forbidden_globs": [".task-evidence/**", "plan.md", "**/plan.md", "decision-log.md", "**/decision-log.md", "build/**", "**/build/**", "screenshots/**", "**/screenshots/**"],
      "forbidden_extensions": [".png", ".jpg", ".jpeg"],
      "allowlist": [],
      "exceptions": {"product assets and test fixtures": ["assets/**", "test/fixtures/**"]}
    }
  }
}
```

Add `mobile-flutter-app` to `repos` as well. `repo_checks` replaces the shared `checks` for the named repo; include any additional required PR checks explicitly. Artifact exceptions should be reviewed against the APP's real asset layout before enabling. These settings are a proposal, not a change to Hub.
