# Issue tracker: GitHub

Issues and specs for this repo live in GitHub Issues on `NatChung/orchd`.
Use the `gh` CLI with an explicit repository. Preserve any existing `GH_TOKEN`;
use repository-scoped credentials without changing the global active account.

## Conventions

- Create: `gh issue create --repo NatChung/orchd --title "..." --body-file <file> --label needs-triage`.
- Read: `gh issue view <number> --repo NatChung/orchd --json number,title,body,labels,comments`.
- List: `gh issue list --repo NatChung/orchd --state open --json number,title,labels`; scope by label as needed.
- Comment: `gh issue comment <number> --repo NatChung/orchd --body-file <file>`.
- Label: `gh issue edit <number> --repo NatChung/orchd --add-label "..."` / `--remove-label "..."`.
- Close: `gh issue close <number> --repo NatChung/orchd` after checking completion evidence.

Use real newlines in body files. Include the problem, reproduction steps where applicable,
expected behavior, and acceptance criteria. Redact credentials, private project names,
account identifiers, local paths, and logs before posting.

For orchd workers, outward writes require a full preview through `orchd ask` and
an explicit answer approving that action. Read-only queries need no account switch.
Exception: when Orch explicitly dispatches a worker to create an issue for
Operator in a `NatChung/*` repository, that issue creation needs no preview.
The exception applies only to the named creation task; PRs, comments, edits,
closures and label maintenance still follow their approval rules. See
[issue-linked workflow](../issue-linked-workflow.md#2-開票權限與-labels).
Read back each write and retain the resulting link.

Before creating an issue, check the mapped labels against the live labels.
If `needs-triage` is missing, omit `--label needs-triage` from the create command
and disclose the missing label and actual applied labels in the preview or
exempt creation report. Do not automatically create the label. If a repository
explicitly requires that label, obtain Operator's decision or complete separately
approved label maintenance before creating the issue.

## Pull requests as a triage surface

**PRs as a request surface: no.** Set to `yes` only if maintainers decide to treat
external PRs as feature requests. Changes still go through a branch and PR review.

## Skill operations

When a skill says "publish to the issue tracker", create a GitHub issue after the
required approval. When it says "fetch the relevant ticket", read the issue above.
Use the canonical role mapping in `triage-labels.md`.
GitHub shares one number space across issues and PRs; resolve ambiguous references
with `gh pr view` and then `gh issue view`, both with `--repo NatChung/orchd`.
