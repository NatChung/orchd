# Release and publication checklist

Use this before making anything public: pushing branches or tags, opening issues or pull requests,
and publishing packages or other artifacts. A scanner hit proves there is something to review; zero
hits do **not** prove there is no sensitive data. Both the automated scan and the human review are
required. This checklist does not claim any credential has been exposed.

## Automated secret scan

`.github/workflows/secret-scan.yml` runs on every push and pull request (and manually):

| Item | Value |
| --- | --- |
| Scanner | [gitleaks](https://github.com/gitleaks/gitleaks) CLI, default rules |
| Version | pinned in `GITLEAKS_VERSION`, release archive verified by `GITLEAKS_SHA256` |
| History scope | `gitleaks git`: every commit reachable from the branches and tags fetched by `actions/checkout` with `fetch-depth: 0` |
| Working tree | `gitleaks dir`: the checked-out files |
| Exceptions | `.gitleaks.toml` only, each naming one rule, the test paths and the exact matched shape |
| Output | redacted findings in the job log; no report artifact is uploaded |

Known limits — review these by hand:

- Only fetched refs are scanned. Fork branches, other contributors' unpushed work, deleted refs,
  `refs/pull/*` heads, and GitHub-side data (issue/PR text, comments, review bodies, release notes,
  wiki, Actions logs, uploaded artifacts) are outside the scan.
- Rules match known token formats and high-entropy assignments. Private names, customer details,
  internal URLs, local paths, low-entropy passwords and personal data are not reliably detected.
- Binary and encoded content (images, archives, base64 blobs) is mostly not inspected.
- An allowlist entry can hide a real value with the same shape; review every change to
  `.gitleaks.toml` as carefully as code.

To reproduce locally, download the pinned release into a new empty directory outside the repo,
verify it against the release `checksums.txt`, and run from the repo root:

```sh
gitleaks git --config .gitleaks.toml --redact --no-banner .
gitleaks dir --config .gitleaks.toml --redact --no-banner .
```

Never paste raw findings into an issue, PR, chat or log. Record only the rule ID, file, commit and
your conclusion. If a finding may be real, report it privately to a maintainer, rotate the credential
first, then remove it from history.

## Manual review before publishing

Record who reviewed, the date, the commit range, the gitleaks version and the result (for example
"0 unreviewed findings; 1 allowlisted test fixture"). Do not record the matched values.

- [ ] **Working tree**: `git status` is clean or every untracked/ignored file is intended to stay
      local. No `.env`, credentials, `gh-accounts.json`, `config.toml`, databases, logs or local
      paths are tracked. Fixtures use fictional names and values.
- [ ] **Commit messages**: read `git log` for the whole range to publish (subjects and bodies,
      including `Co-Authored-By` and trailers) for private names, internal URLs, issue links to
      private trackers and pasted output.
- [ ] **All revisions to be published**: the scan covered every branch and tag you will push, not
      only the final tree. A secret removed in a later commit is still public in the earlier one.
      `git log --all --stat` and `git diff` the full range for files that were added and later deleted.
- [ ] **Issue and PR text**: titles, bodies, comments, review replies and linked logs are redacted
      (see CONTRIBUTING.md). Screenshots and attached files are checked too.
- [ ] **Artifacts**: built wheels/sdists (`unzip -l`, `tar -tzf`), release assets and Actions
      artifacts contain only intended files; no `build/`, caches or local configuration.
- [ ] **Scan record**: gitleaks version, scope (refs and paths) and limits above are recorded with
      the human review result; raw hits are not published.
- [ ] **License and redistribution rights**: the repository LICENSE covers the release; every copied
      or quoted third-party file, template, image or text has a compatible license or explicit
      permission. Link to material instead of copying it when rights are unclear; material with
      unclear rights is not published.
- [ ] **Workflow permissions**: every workflow declares `permissions: contents: read` (or narrower)
      at the top level and widens only per job with a written reason; checkouts use
      `persist-credentials: false` unless a job must push; official actions are pinned to a major
      tag and third-party actions to a full commit SHA; secrets are not exposed to `pull_request`
      runs from forks.
