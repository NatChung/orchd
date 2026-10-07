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
Read back each write and retain the resulting link.

## Pull requests as a triage surface

**PRs as a request surface: no.** Set to `yes` only if maintainers decide to treat
external PRs as feature requests. Changes still go through a branch and PR review.

## Skill operations

When a skill says "publish to the issue tracker", create a GitHub issue after the
required approval. When it says "fetch the relevant ticket", read the issue above.
Use the canonical role mapping in `triage-labels.md`.
GitHub shares one number space across issues and PRs; resolve ambiguous references
with `gh pr view` and then `gh issue view`, both with `--repo NatChung/orchd`.
