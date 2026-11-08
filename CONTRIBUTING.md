# Contributing

## Where to report

Use [GitHub Issues on this repository](https://github.com/NatChung/orchd/issues) for orchd itself: usage questions
and problems, bugs, and feature requests. One issue per problem or request.

Your own work is not an orchd issue. Tasks you hand to an Orch or worker, their questions, progress and reports stay
in your local orchd task records (the SQLite database under the state directory) and in your own project trackers.
Open an orchd issue only when orchd's behavior is the problem, and describe it with synthetic data.

## Report format

Include:

- **Version**: the orchd version and commit (`uv tool list` shows the installed version; for a checkout,
  `git rev-parse --short HEAD`), plus the Python, Claude Code and Codex CLI versions involved.
- **Installation**: `uv tool install` from Git, `scripts/install-windows.ps1`, or a checkout's `bin/orchd`.
- **OS**: macOS or Windows version, and the shell (zsh, bash, PowerShell).
- **Steps to reproduce**: the commands or MCP calls, in order, from a fresh state where possible.
- **Expected behavior** and **actual behavior**.
- **Logs**: the relevant `orchd doctor` lines, command output or error text, sanitized as described below. Trim to
  the part that shows the problem.

Feature requests describe the problem first, then the proposed behavior and how to tell it is done.

## Keep public content clean

Public issues, pull requests, commits and logs must exclude credentials, tokens, customer names or identifiers,
personal information, private project or account names, internal URLs, connector settings, and machine-specific
paths (write `~/projects/example-repo`, not your real home directory). Use fictional fixtures.

Keep sensitive operational details in an access-controlled private tracker; existing private users can use
orchd-archive. Link a sanitized public issue only when the general problem can be described safely. Never include
secrets in either tracker; report a possible exposure privately to a maintainer and rotate the affected credentials.
Link to third-party material instead of copying it unless redistribution rights are established.

## Triage

New issues start with `needs-triage`. A maintainer then moves each issue to one of the other four labels
([label mapping](docs/agents/triage-labels.md)):

| Label | Meaning | What happens next |
| --- | --- | --- |
| `needs-triage` | Not evaluated yet. | A maintainer reads it and picks one of the labels below. |
| `needs-info` | Cannot be acted on as written. | The maintainer comments with what is missing (usually a field from the report format). The reporter adds it, and the issue returns to `needs-triage`. An issue left without the information may be closed. |
| `ready-for-agent` | Fully specified, with acceptance criteria an AI worker can check. | Any contributor or an orchd worker may implement it. |
| `ready-for-human` | Needs a person: a design decision, manual or hardware testing, or access an agent lacks. | A person implements it or splits it into agent-ready issues. |
| `wontfix` | Will not be done. | Closed with a short reason. |

An issue closes when the pull request that resolves it is merged and every acceptance criterion has been checked, or
as `wontfix`. A closing comment links the evidence (PR, test output, or reason).

## Pull requests

1. Work on an isolated branch (or Git worktree), never directly on `main`.
2. Reference the issue in the PR description (`Closes #N` when it resolves it) and list how each acceptance
   criterion was checked.
3. Run `python3 -m unittest discover -s tests` before submitting code changes and include the result.
4. Check every changed file and commit message for private data.
5. A reviewer other than the author reviews the PR against the issue's acceptance criteria. When the author is an AI
   worker, the review runs as a separate task with a different worker (preferably another model family), and the
   author does not merge its own PR.
6. The reviewer records the verdict as a PR comment naming the reviewed commit. Merge only after a passing
   review of the current head; answer or fix every finding first.

See [review/merge policy](docs/review-merge-policy.md) and [issue tracker conventions](docs/agents/issue-tracker.md).
