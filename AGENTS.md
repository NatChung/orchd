# Contributor instructions

Run `python3 -m unittest discover -s tests` before submitting code changes.
Use an isolated branch and submit a pull request for review.
Keep credentials and machine-specific configuration outside this repository.
Architecture and terminology live in CONTEXT.md and docs/adr/.

## Agent skills

### Issue tracker

Issues and specs live in GitHub Issues on `NatChung/orchd`. See `docs/agents/issue-tracker.md`.

### Triage labels

Use the five default triage labels, each named after its canonical role. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `GLOSSARY.md` points to the existing vocabulary in `CONTEXT.md`; decisions live in `docs/adr/`. See `docs/agents/domain.md`.
